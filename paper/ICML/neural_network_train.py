import torch
import numpy as np
import os
from collections import Counter
from copy import deepcopy, copy
from random import choice
from uuid import uuid4
from dataset_settings import SETTINGS as DATASETS, LOG_DISTRIBUTIONS, INTEGERS
from torchvision import transforms
from torch.utils.data import Dataset, DataLoader, TensorDataset
from tools import IndexDataset, preload_dataset, compute_size
from torch.autograd import Variable
from dynnet.utils import shrinknet_penalty, update_statistics
from utils.measures import TrainingStats
from utils.misc import tn
from multiprocessing import cpu_count
from torch.optim import Adam, SGD, Adamax
from torch.optim.lr_scheduler import ReduceLROnPlateau
from dynnet.filters import SimpleFilter
import sys

def clean_params(params):
    result = {}
    for key, value in params.items():
        if INTEGERS.match(key) is not None:
            value = int(value)
        result[key] = value
    return result

def sample_params(params, full_config=True):
    if full_config:
        full_params = copy(params)
        params = params['params']
    result = {}
    for key, value in params.items():
        if isinstance(value, list):
            result[key] = choice(value)
        elif isinstance(value, tuple):
            assert len(value) == 2, 'only allow ranges'
            value = np.array(value)
            is_log = LOG_DISTRIBUTIONS.match(key) is not None
            if is_log:
                value = np.log(value)
            sampled = np.random.uniform(*value)
            if is_log:
                sampled = np.exp(sampled)
            result[key] = sampled
    result = clean_params(result)
    if full_config:
       full_params['params'] = result
       return full_params
    return result

def prepare_loaders(config, split=0.8):
    data_augmentations = config['data_augmentations']
    dataset = config['dataset']
    normalization_params = config['normalization']
    basic_transforms = []
    data_augmentations_transforms = []
    if data_augmentations:
        data_augmentations_transforms.extend(data_augmentations)
    if normalization_params:
        basic_transforms.append(transforms.Normalize(*normalization_params))
        data_augmentations_transforms.append(transforms.Normalize(*normalization_params))


    full_train_dataset_simple = dataset(train=True, transform=basic_transforms)
    full_train_dataset_augmented = dataset(train=True, transform=data_augmentations_transforms)
    testing_dataset = dataset(train=False, transform=basic_transforms)

    indices = np.arange(0, len(full_train_dataset_simple))
    np.random.shuffle(indices)
    boundary = int(len(indices) * split)
    train_indices = indices[:boundary]
    validation_indices = indices[boundary:]

    training_dataset = IndexDataset(full_train_dataset_augmented, train_indices)
    validation_dataset = IndexDataset(full_train_dataset_simple, validation_indices)


    weights = [0] * config['params']['output_features']

    for _, labels in DataLoader(training_dataset, batch_size=100000, num_workers=cpu_count()):
        for label in labels.cpu().numpy():
            weights[label] +=1
    class_weights = torch.from_numpy(1 / np.array(weights) / len(weights) * len(training_dataset)).float().cuda()

    if data_augmentations:
        training_dataloader = DataLoader(training_dataset,
                                         batch_size=config['params']['batch_size'],
                                         pin_memory=True,
                                         shuffle=True,
                                         num_workers=cpu_count())
    else:
        training_dataloader = preload_dataset(training_dataset,
                                              config['params']['batch_size'])
    validation_dataloader = preload_dataset(validation_dataset,
                                            config['val_batch_size'])
    testing_dataloader = preload_dataset(testing_dataset,
                                         config['val_batch_size'])
    return training_dataloader, validation_dataloader, testing_dataloader, class_weights

def forward(model, dataloader, config, class_weights, optimizer=None):
    accs = []
    losses = []
    is_classification = config['mode'] == 'classification'
    if is_classification:
            criterion = torch.nn.CrossEntropyLoss(weight=class_weights)
    else:
        criterion = torch.nn.MSELoss()
    lamb = config['params']['lambda']

    for i, (inputs, labels) in enumerate(dataloader):
        inputs = Variable(inputs.cuda(async=True), volatile=(optimizer is None))
        labels = Variable(labels.cuda(async=True), volatile=(optimizer is None))
        prediction = model(inputs)
        loss = criterion(prediction, labels)
        losses.append(tn(loss.data))
        penalty = float(lamb) * shrinknet_penalty(model)
        loss = loss + penalty
        if is_classification:
            discrete_prediction = prediction.max(1)[1]
            accuracy = (discrete_prediction == labels).float().data.mean()
            accs.append(accuracy)
        if optimizer is not None:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            update_statistics(model)
    losses = np.array(losses).mean()
    if accs:
        return losses, np.array(accs).mean()
    return losses, losses

def init_model(model):
    for parameter in model.parameters():
        if len(parameter.size()) > 1:
            torch.nn.init.kaiming_uniform(parameter.data)
    for module in model.modules():
        if hasattr(module, 'running_mean') and hasattr(module, 'running_var'):
            module.running_mean.fill_(0)
            module.running_var.fill_(0.9)

def split_params(model):
    filter_parameters = set()
    for module in model.modules():
        if isinstance(module, SimpleFilter):
            for p in module.parameters():
                filter_parameters.add(p)
    other_parameters = set(model.parameters()) - filter_parameters
    return other_parameters, filter_parameters

def train(config, epochs=400):

    stats = TrainingStats()
    is_classification = config['mode'] == 'classification'
    model = config['model'](config['params']).cuda()
    init_model(model)
    dl_train, dl_val, dl_test, class_weights = prepare_loaders(config)
    weight_decay = config['params']['weight_decay']
    lr = config['params']['learning_rate']
    other_parameters, filter_parameters = split_params(model)
    optimizer = Adam([{
        'params': other_parameters,
        'lr': lr,
        'weight_decay': weight_decay # Do not apply weight decay on filters
    }, {
        'params': filter_parameters,
        'lr': lr,
    }])

    if is_classification:
        direction = 'max'
    else:
        direction = 'min'
    scheduler = ReduceLROnPlateau(optimizer, direction, patience=5, factor=0.1, verbose=True)
    try:
        for _ in range(epochs):
            stats.next_epoch()
            model.train()
            with stats.time('training'):
                train_loss, train_acc = forward(model, dl_train, config, class_weights, optimizer)
            stats.log('train_loss', train_loss)
            stats.log('train_acc', train_acc)
            model.eval()
            if config['params']['dynamic']:
                with stats.time('garbage_collect'):
                    log = model.garbage_collect()
                with stats.time('optimizer_update'):
                    log.update_optimizer(optimizer)
                sizes = compute_size(model)
                print(sizes)
                for i, value, in enumerate(sizes):
                    stats.log('size_%s' % (i + 1), value)
            with stats.time('inference_val'):
                val_loss, val_acc = forward(model, dl_val, config, class_weights)
            stats.log('val_loss', val_loss)
            stats.log('val_acc', val_acc)
            with stats.time('inference_test'):
                test_loss, test_acc = forward(model, dl_test, config, class_weights)
                stats.log('test_loss', test_loss)
                stats.log('test_acc', test_acc)
            print(train_acc, test_acc)
            stats.log('learning_rate', optimizer.param_groups[0]['lr'])
            scheduler.step(val_acc)
            if optimizer.param_groups[0]['lr'] < 1e-6:
                break
    except:
        raise
    return stats, model

def train_simple_CIFAR10_dynamic():
    config = deepcopy(sample_params(DATASETS['CIFAR10_VGG_DYNAMIC']))
    print(config)
    return
    stats, model = train(config)
    with open('./experiments_results/simple_dynamic_CIFAR10_%s.pkl' % i, 'wb') as f:
        torch.save((config, stats), f)

def train_simple_CIFAR10_static():
    for i in range(50, 100):
        config = deepcopy(sample_params(DATASETS['CIFAR10_VGG_DYNAMIC']))
        config['params']['batch_norm'] = True
        config['params']['dynamic'] = False
        config['params']['weight_decay'] = 0.000001
        config['params']['lambda'] = 0.0001
        config['params']['factor'] = 1
        config['params']['batch_size'] = 150
        config['params']['learning_rate'] = 1e-2
        stats, model = train(config)
        with open('./experiments_results/simple_static_CIFAR10_%s.pkl' %i, 'wb') as f:
            torch.save((config, stats), f)

def random_train(mode):
    sampling_settings = DATASETS[mode]
    config = deepcopy(sample_params(sampling_settings))
    print(mode)
    print(config)
    filename_stats = './experiments_results/random_search/%s/%s.pkl' % (mode, uuid4())
    filename_model = './experiments_results/random_search/%s/%s.model' % (mode, uuid4())
    os.makedirs(os.path.dirname(filename_stats), exist_ok=True)
    os.makedirs(os.path.dirname(filename_model), exist_ok=True)
    stats, model = train(config)
    with open(filename_stats, 'wb') as f:
        torch.save((sampling_settings, config, stats), f)
    with open(filename_model, 'wb') as f:
        torch.save(model.cpu(), f)

if __name__ == '__main__':
    random_train(sys.argv[-1])
