import torch
import numpy as np
from torch import nn
from torch.nn.functional import linear, relu, conv2d
from torch.nn import Module, Parameter, ParameterList
from torch.autograd import Function, Variable
from torch.nn.modules.conv import _single, _pair, _triple
from utils.misc import tn
from torchvision.datasets import MNIST, FashionMNIST

def walk_graph_feature_ids(node):
    if hasattr(node, 'get_feature_ids'):
        return node.get_feature_ids()
    if hasattr(node, 'grad_fn'):
        return walk_graph_feature_ids(node.grad_fn)
    if hasattr(node, 'next_functions') and len(node.next_functions) == 1:
        return walk_graph_feature_ids(node.next_functions[0][0])
    return node

def compute_patch(old_features, new_features):
    old_features = old_features.numpy()
    new_features = new_features.numpy()
    selector = np.where(np.isin(old_features, new_features))
    if not np.all(old_features[selector] == new_features[np.isin(new_features, old_features)]):
        raise NotImplementedError('Feature Reorder is not supported')
    if len(selector[0]) == 0:
        return torch.zeros(0).long()
    return torch.from_numpy(selector[0])

class FeaturesIdProvider(Function):
    def __init__(self, feature_ids):
        self._feature_ids = feature_ids

    def forward(self,x):
        return x + 0 # Otherwise the function is optimized out...

    def backward(self, grad_output):
        return grad_output

    def get_feature_ids(self):
        return self._feature_ids

def default_initializer(tensor):
    return tensor.normal_()

class DynamicModule(Module):

    def __init__(
        self,
        in_features=None,
        out_features=None,
        weight_allocation=(),
        weight_initializer=default_initializer,
        bias_initializer=default_initializer,
        reuse_features=True,
        bias=True
    ):
        super(DynamicModule, self).__init__()

        # Saving parameters
        self.in_features = in_features
        self.out_features = out_features
        self.weight_allocation = weight_allocation
        self.weight_initializer = weight_initializer
        self.bias_initializer = bias_initializer
        self.reuse_features = reuse_features
        self.has_bias = bias

        if self.out_features is not None:
            # We have a fixed number of features out so we precompute it
            self._out_feature_ids = torch.arange(0, self.out_features).long()
        else:  # We will need to define it later
            self._out_features_id = None

        if self.in_features is not None:
            self._in_feature_ids = torch.arange(0, self.in_features).long()
        else:
            self._in_feature_ids = None

        # State flags
        self.collecting = False
        self.growing = False
        self.max_feature = 0

        # Feature ids tracker
        self.in_features_map = []
        self.out_features_map = []

        # Weight Parameters
        self.weight_blocks = ParameterList()

        # Bias parameters
        if self.has_bias:
            if self.out_features is None:
                self.bias_blocks = ParameterList()
            else:
                bias = self.bias_initializer(torch.zeros(self.out_features))
                self.bias_weights = Parameter(bias)

        # Filter Parameter
        if self.out_features is None:
            self.filters_blocks = ParameterList()

        # Device information
        self.device_id = -1 # CPU

    def garbage_collect(self):
        if self.collecting is not False:
            raise AssertionError('Already collecting, do at least one pass')
        self.collecting = True
        return self

    def set_device_id(self, device_id):
        self.device_id = device_id

    def wrap(self, tensor):
        if self.device_id == -1:
            return tensor.cpu()
        else:
            return tensor.cuda(self.device_id)

    def grow(self, size=0):
        if self.growing is not False:
            raise AssertionError('Already growing, do at least one pass')
        self.growing = size
        return self

    def regenerate_out_feature_ids(self):
        if self.out_features is None:
            non_empty_features = [x for x in self.out_features_map if len(x) > 0]
            if len(non_empty_features) == 0:
                self._out_feature_ids = self.wrap(torch.zeros(0).long())
            else:
                self._out_feature_ids = torch.cat(non_empty_features)

    def block_parameters(self, block_id=-1):  # By default the last block
        result = []
        if len(self.weight_blocks) == 0:
            return result
        result.append(self.weight_blocks[block_id])
        if hasattr(self,'bias_blocks'):
            result.append(self.bias_blocks[block_id])
        elif hasattr(self, 'bias_weights'):
            result.append(self.bias_weights)
        if hasattr(self, 'filters_blocks'):
            result.append(self.filters_blocks[block_id])
        return (x for x in result if len(x) > 0)  # remove dead parameters

    def grow_now(self, feature_ids):
        # Compute the number of rows (outputs) in the weight matrix
        if self.out_features is None:
            rows = self.growing
            out_feature_ids = torch.arange(self.max_feature, self.max_feature + rows)
            self.out_features_map.append(out_feature_ids)
            self.max_feature += rows
        else:
            rows = self.out_features

        # Compute the number of columns (inputs) in the weight matrix
        if self.in_features is not None:
            cols = self.in_features
        else:
            if self.reuse_features or len(self.in_feature_maps) == 0:
                new_features = feature_ids.clone()
            else:
                m = self.in_feature_maps[-1].max()
                new_features = feature_ids[feature_ids > m]
            cols = len(new_features)
            self.in_features_map.append(new_features)

        weights = self.wrap(self.weight_initializer(
            torch.zeros((rows, cols) + self.weight_allocation)
        ))
        self.weight_blocks.append(Parameter(weights))

        if hasattr(self, 'bias_blocks'):
            bias = self.wrap(self.bias_initializer(torch.zeros(rows)))
            self.bias_blocks.append(Parameter(bias))

        if hasattr(self, 'filters_blocks'):
            filter = self.wrap(torch.ones(rows))
            self.filters_blocks.append(Parameter(filter))

        if self.out_features is None:
            new_feature_ids = []

        self.growing = False
        self.regenerate_out_feature_ids()

    def collect_now(self, feature_ids):
        if not self.collecting:
            return

        # Collect old inputs
        if self.in_features is None: # Only process dynamic input models
            new_feature_ids = []
            new_weight_blocs = ParameterList()
            for old_features, weights in zip(self.in_features_map, self.weight_blocks):
                patch = compute_patch(old_features, feature_ids)
                empty = torch.zeros(0)
                if len(patch) == 0: # dead block
                    new_feature_ids.append(empty.long())
                    new_weights = self.wrap(empty)
                else:
                    new_feature_ids.append(old_features[patch])
                    patch = self.wrap(patch)
                    last_dim = len(weights.size()) - 1
                    new_weights = weights.data.transpose(0, last_dim)[patch].transpose(0, last_dim)
                new_weight_blocs.append(Parameter(new_weights))
            self.weight_blocks = new_weight_blocs
            self.in_features_map = new_feature_ids

        # Collect unused features
        if self.out_features is None: # Only process dynamicoutput models
            new_feature_ids = []
            new_weight_blocs = ParameterList()
            new_filters_blocks = ParameterList()
            new_bias_blocks = ParameterList()
            source = zip(self.out_features_map,
                self.weight_blocks, self.filters_blocks)
            for i, (old_features, weights, filter) in enumerate(source):
                patch = torch.nonzero(filter.data > 0).squeeze()
                new_bias = None
                if len(patch) == 0 or len(weights) == 0:
                    empty = torch.zeros(0)
                    new_weights = empty
                    new_filter = empty
                    new_feature_ids.append(empty.long())
                    if self.has_bias:
                        new_bias = empty
                else:
                    new_feature_ids.append(old_features[patch.cpu()])
                    new_weights = weights.data[patch]
                    new_filter = filter.data[patch]
                    if self.has_bias:
                        bias = self.bias_blocks[i]
                        new_bias = bias.data[patch]
                new_weight_blocs.append(Parameter(new_weights))
                new_filters_blocks.append(Parameter(new_filter))
                if new_bias is not None:
                    new_bias_blocks.append(Parameter(new_bias))

            self.out_features_map = new_feature_ids
            self.filters_blocks = new_filters_blocks
            self.weight_blocks = new_weight_blocs
            if hasattr(self, 'bias_blocks'):
                self.bias_blocks = new_bias_blocks

        self.regenerate_out_feature_ids()
        self.collecting = False

    def compute(self, x):
        # Generate input tensors
        inputs = []
        if len(self.in_features_map) == 0:
            inputs = [x] * len(self.weight_blocks)
        else:
            start_idx = 0
            for features in self.in_features_map:
                if len(features) == 0:
                    inputs.append(self.wrap(torch.zeros(0)))
                else:
                    end_idx = start_idx + len(features)
                    inputs.append(x[:, start_idx:end_idx])
                    if not self.reuse_features:
                        start_idx += len(features)

        # Process the inputs
        results = []
        for i, (inp, weights) in enumerate(zip(inputs, self.weight_blocks)):
            bias = None
            if self.has_bias:
                if self.out_features is None:
                    bias = self.bias_blocks[i]
                else:
                    bias = self.bias_weights
            if len(weights) != 0:
                result = self.compute_block(inp, weights, bias)
                if hasattr(self, 'filters_blocks'): # Apply filter if needed
                    filter = self.filters_blocks[i]
                    last_dim = len(result.size()) - 1
                    result = result.transpose(1, last_dim)
                    result = result * relu(filter)
                    result = result.transpose(1, last_dim)
                results.append(result)

        # Merge the results properly
        if len(results) == 0:
            raise AssertionError('Empty Model, call model.grow(size)')
        elif self.out_features is None:
            return torch.cat(results, dim=1)
        else:
            return torch.stack(results, dim=0).sum(0)

    def compute_block(self, x, weights, bias=None):
        raise NotImplementedError('Should be implemented by subclasses')

    def loss_factor(self):
        return float(np.array(self.weight_allocation).prod())

    def full_filter(self):
        individual_filters = [relu(x) for x in self.filters_blocks if len(x) > 0]
        if len(individual_filters) == 0:
            return Variable(self.wrap(torch.zeros(0)))
        return torch.cat(individual_filters)

    @property
    def l1_loss(self):
        if hasattr(self, 'filters_blocks') and len(self.filters_blocks) > 0:
            return self.full_filter().sum() * self.loss_factor()
        return Variable(self.wrap(torch.zeros(1)), requires_grad=False)

    @property
    def num_output_features(self):
        if self.out_features is not None:
            return self.out_features
        if len(self.filters_blocks) == 0:
            return 0
        return (self.full_filter().data > 0).long().sum()

    @property
    def num_input_features(self):
        if self.in_features is not None:
            return self.in_features
        if len(self.in_features_map) == 0:
            return 0
        return len(set().union(*(set(x.numpy()) for x in self.in_features_map)))

    def generate_input(self, additional_dims=()):
        if self.in_features is None:
            raise ValueError('fake pass needs input or in_feature defined')
        size = (1, self.in_features) + additional_dims
        x = torch.rand(*size)
        x = torch.autograd.Variable(x, requires_grad=False)
        return self.wrap(x)

    def forward(self, x=None):
        if x is None:
            x = self.generate_input()
        # Check if any resize operation scheduled
        if self.collecting or self.growing is not False:
            if self._in_feature_ids is None:
                feature_ids = walk_graph_feature_ids(x)
            else:
                feature_ids = self._in_feature_ids

            if self.collecting:
                self.collect_now(feature_ids)
            if self.growing is not False:
                self.grow_now(feature_ids)

        if len(self.weight_blocks) == 0:
            raise AssertionError('Empty Model, call model.grow(size)')

        x = self.compute(x)

        # Attach feature Ids to the graph
        x = FeaturesIdProvider(self._out_feature_ids)(x)

        return x

    @property
    def current_dimension_repr(self):
        return " [%s -> %s]" % (self.num_input_features, self.num_output_features)

class Linear(DynamicModule):

    def __init__(self, in_features=None, out_features=None, bias=True, weight_initializer=default_initializer, bias_initializer=default_initializer, reuse_features=True):
        super(Linear, self).__init__(
            in_features=in_features,
            out_features=out_features,
            weight_initializer=weight_initializer,
            bias_initializer=bias_initializer,
            weight_allocation=(),
            bias=bias,
            reuse_features=reuse_features
        )

    def __repr__(self):
        inputs = '?' if self.in_features == None else self.in_features
        outputs = '?' if self.out_features == None else self.out_features
        return "Linear* (%s -> %s, bias=%s)" % (inputs, outputs, self.has_bias) + self.current_dimension_repr

    def compute_block(self,x, weights, bias):
        return linear(x, weights, bias)

class Conv2d(DynamicModule):
    def __init__(self, kernel_size,stride=1, in_channels=None, out_channels=None,
                 padding=0, dilation=1, groups=1, bias=True, reuse_features=True,
                 weight_initializer=default_initializer,
                 bias_initializer=default_initializer
                 ):

        self.stride = _pair(stride)
        self.padding = _pair(padding)
        self.dilation = _pair(dilation)
        self.groups = groups
        self.kernel_size = _pair(kernel_size)
        self.output_padding = _pair(0)
        self.bias = bias

        super(Conv2d, self).__init__(
            in_features=in_channels,
            out_features=out_channels,
            weight_initializer=weight_initializer,
            bias_initializer=bias_initializer,
            weight_allocation=self.kernel_size,
            reuse_features=reuse_features,
            bias=bias
        )

    def compute_dims(self):
        self.in_channels = self.in_features if self.in_features is not None else '?'
        self.out_channels = self.out_features if self.out_features is not None else '?'

    def compute_block(self, x, weights, bias):
        return conv2d(x, weights, bias, self.stride, self.padding, self.dilation, self.groups)

    def __repr__(self):
        self.compute_dims()
        return torch.nn.Conv2d.__repr__(self) + self.current_dimension_repr

if __name__ == '__main__':
    model = Linear(in_features=10).grow(10)
    data = model.generate_input()
    model2 = Linear().grow(5)
    model2(model(data))
    model.grow(10)
    model2.grow(3)
    model2(model(data))
    model.filters_blocks[0].data.zero_()
    model.garbage_collect()
    model2.garbage_collect()
    model2(model(data))