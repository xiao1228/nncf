# Copyright (c) 2023 Intel Corporation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from typing import Dict, List, Optional, Type, TypeVar

from nncf.common.graph.definitions import NNCFGraphNodeType
from nncf.common.graph.layer_attributes import BaseLayerAttributes
from nncf.common.graph.layer_attributes import ConvolutionLayerAttributes
from nncf.common.graph.operator_metatypes import INPUT_NOOP_METATYPES
from nncf.common.graph.operator_metatypes import NOOP_METATYPES
from nncf.common.graph.operator_metatypes import OUTPUT_NOOP_METATYPES
from nncf.common.graph.operator_metatypes import OperatorMetatype
from nncf.common.graph.operator_metatypes import OperatorMetatypeRegistry
from nncf.common.hardware.opset import HWConfigOpName
from nncf.torch.dynamic_graph.graph import DynamicGraph
from nncf.torch.dynamic_graph.structs import NamespaceTarget

ModuleAttributes = TypeVar("ModuleAttributes", bound=BaseLayerAttributes)

PT_OPERATOR_METATYPES = OperatorMetatypeRegistry("operator_metatypes")


class PTOperatorMetatype(OperatorMetatype):
    """
    Base class for grouping PyTorch operators based on their semantic meaning.
    Each derived class represents a single semantic group - for example, AddMetatype would
    group together '__iadd__', '__add__' and '__radd__' operations which all define nodewise
    tensor addition.
    Derived classes also specify which PyTorch functions in which modules should be patched
    so that the entire group of operations is visible in the internal graph.
    Grouping also allows efficient application of HW specifics to compression of
    certain operation groups.

    :param external_op_names: Names of functions registered as operators via @register_operator to be associated
    with this metatype.
    :param module_to_function_names: Names of functions from 'torch.nn.function', 'torch.tensor' and 'torch' modules
    respectively, which are associated with this metatype.
    :param subtypes: List of subtypes of PyTorch operator.
    """

    external_op_names: List[str] = []

    module_to_function_names: Dict[NamespaceTarget, List[str]] = {
        NamespaceTarget.TORCH_NN_FUNCTIONAL: [],
        NamespaceTarget.TORCH_TENSOR: [],
        NamespaceTarget.TORCH: [],
    }

    subtypes: List[Type["PTOperatorMetatype"]] = []

    @classmethod
    def get_subtypes(cls) -> List[Type["PTOperatorMetatype"]]:
        return cls.subtypes.copy()

    @classmethod
    def get_all_namespace_to_function_names(cls) -> Dict[NamespaceTarget, List[str]]:
        output = dict(cls.module_to_function_names)
        output[NamespaceTarget.EXTERNAL] = cls.external_op_names
        return output

    @classmethod
    def get_all_aliases(cls) -> List[str]:
        output = set()
        for _, function_names in cls.module_to_function_names.items():
            output = output.union(function_names)
        if cls.external_op_names is not None:
            output = output.union(cls.external_op_names)
        return list(output)

    @classmethod
    def determine_subtype(
        cls, layer_attributes: Optional[BaseLayerAttributes] = None, function_args=None, functions_kwargs=None
    ) -> Optional["PTOperatorSubtype"]:
        matches = []
        for subtype in cls.get_subtypes():
            if subtype.matches(layer_attributes, function_args, functions_kwargs):
                matches.append(subtype)
        assert len(matches) <= 1, "Multiple subtypes match operator call - cannot determine single subtype."
        if not matches:
            return None

        subtype = matches[0]
        nested_subtype = subtype.determine_subtype(layer_attributes, function_args, functions_kwargs)
        if nested_subtype:
            return nested_subtype
        return subtype


class PTOperatorSubtype(PTOperatorMetatype):
    """
    Exact specialization of PTOperatorMetatype that can only be determined via operator argument
    inspection or owning module attribute inspection, and that may have specialized compression method
    configuration other than the one used for general operations having the type of PTOperatorMetatype.
    """

    @classmethod
    def matches(
        cls, layer_attributes: Optional[BaseLayerAttributes] = None, function_args=None, functions_kwargs=None
    ) -> bool:
        raise NotImplementedError


class PTModuleOperatorSubtype(PTOperatorSubtype):
    @classmethod
    def matches(
        cls, layer_attributes: Optional[BaseLayerAttributes] = None, function_args=None, functions_kwargs=None
    ) -> bool:
        key = DynamicGraph.IS_CALLED_INSIDE_NNCF_MODULE
        if functions_kwargs is None or key not in functions_kwargs:
            return False
        return functions_kwargs[key]


class PTDepthwiseConvOperatorSubtype(PTOperatorSubtype):
    @classmethod
    def matches(
        cls, layer_attributes: Optional[BaseLayerAttributes] = None, function_args=None, functions_kwargs=None
    ) -> bool:
        if not isinstance(layer_attributes, ConvolutionLayerAttributes):
            return False
        if layer_attributes.groups == layer_attributes.in_channels and layer_attributes.in_channels > 1:
            return True
        return False


@PT_OPERATOR_METATYPES.register()
@INPUT_NOOP_METATYPES.register()
class PTInputNoopMetatype(PTOperatorMetatype):
    name = "input_noop"
    external_op_names = [name, NNCFGraphNodeType.INPUT_NODE]


@PT_OPERATOR_METATYPES.register()
@OUTPUT_NOOP_METATYPES.register()
class PTOutputNoopMetatype(PTOperatorMetatype):
    name = "output_noop"
    external_op_names = [name, NNCFGraphNodeType.OUTPUT_NODE]


@PT_OPERATOR_METATYPES.register()
@NOOP_METATYPES.register()
class PTNoopMetatype(PTOperatorMetatype):
    name = "noop"
    external_op_names = [name]
    module_to_function_names = {
        NamespaceTarget.TORCH_NN_FUNCTIONAL: [],
        NamespaceTarget.TORCH_TENSOR: [],
        NamespaceTarget.TORCH: ["contiguous", "clone"],
    }


@PT_OPERATOR_METATYPES.register()
class PTDepthwiseConv1dSubtype(PTDepthwiseConvOperatorSubtype):
    name = "Conv1DOp"
    hw_config_name = [HWConfigOpName.DEPTHWISECONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv1d"]}
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTModuleConv1dMetatype(PTModuleOperatorSubtype):
    name = "Conv1DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv1d"]}
    subtypes = [PTDepthwiseConv1dSubtype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTConv1dMetatype(PTOperatorMetatype):
    name = "Conv1DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv1d"]}
    subtypes = [PTModuleConv1dMetatype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTDepthwiseConv2dSubtype(PTDepthwiseConvOperatorSubtype):
    name = "Conv2DOp"
    hw_config_names = [HWConfigOpName.DEPTHWISECONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv2d"]}
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTModuleConv2dMetatype(PTModuleOperatorSubtype):
    name = "Conv2DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv2d"]}
    subtypes = [PTDepthwiseConv2dSubtype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTConv2dMetatype(PTOperatorMetatype):
    name = "Conv2DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv2d"]}
    subtypes = [PTModuleConv2dMetatype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTDepthwiseConv3dSubtype(PTDepthwiseConvOperatorSubtype):
    name = "Conv3DOp"
    hw_config_names = [HWConfigOpName.DEPTHWISECONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv3d"]}
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTModuleConv3dMetatype(PTModuleOperatorSubtype):
    name = "Conv3DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv3d"]}
    subtypes = [PTDepthwiseConv3dSubtype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTConv3dMetatype(PTOperatorMetatype):
    name = "Conv3DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv3d"]}
    subtypes = [PTModuleConv3dMetatype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTModuleConvTranspose1dMetatype(PTModuleOperatorSubtype):
    name = "ConvTranspose1DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv_transpose1d"]}
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTConvTranspose1dMetatype(PTOperatorMetatype):
    name = "ConvTranspose1DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv_transpose1d"]}
    subtypes = [PTModuleConvTranspose1dMetatype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTModuleConvTranspose2dMetatype(PTModuleOperatorSubtype):
    name = "ConvTranspose2DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv_transpose2d"]}
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTConvTranspose2dMetatype(PTOperatorMetatype):
    name = "ConvTranspose2DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv_transpose2d"]}
    subtypes = [PTModuleConvTranspose2dMetatype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTModuleConvTranspose3dMetatype(PTModuleOperatorSubtype):
    name = "ConvTranspose3DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv_transpose3d"]}
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTConvTranspose3dMetatype(PTOperatorMetatype):
    name = "ConvTranspose3DOp"
    hw_config_names = [HWConfigOpName.CONVOLUTION]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["conv_transpose3d"]}
    subtypes = [PTModuleConvTranspose3dMetatype]
    output_channel_axis = 1


@PT_OPERATOR_METATYPES.register()
class PTModuleDeformConv2dMetatype(PTModuleOperatorSubtype):
    name = "DeformConv2dOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["deform_conv2d"]}


@PT_OPERATOR_METATYPES.register()
class PTDeformConv2dMetatype(PTOperatorMetatype):
    name = "DeformConv2dOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["deform_conv2d"]}
    subtypes = [PTModuleDeformConv2dMetatype]


@PT_OPERATOR_METATYPES.register()
class PTModuleLinearMetatype(PTModuleOperatorSubtype):
    name = "LinearOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["linear"], NamespaceTarget.TORCH: ["addmm"]}
    hw_config_names = [HWConfigOpName.MATMUL]
    output_channel_axis = -1


@PT_OPERATOR_METATYPES.register()
class PTLinearMetatype(PTOperatorMetatype):
    name = "LinearOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["linear"], NamespaceTarget.TORCH: ["addmm"]}
    hw_config_names = [HWConfigOpName.MATMUL]
    subtypes = [PTModuleLinearMetatype]
    output_channel_axis = -1


@PT_OPERATOR_METATYPES.register()
class PTHardTanhMetatype(PTOperatorMetatype):
    name = "HardTanhOP"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["hardtanh"]}


@PT_OPERATOR_METATYPES.register()
class PTHardSwishMetatype(PTOperatorMetatype):
    name = "HardSwishOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["hardswish"]}


@PT_OPERATOR_METATYPES.register()
class PTHardSigmoidMetatype(PTOperatorMetatype):
    name = "HardSigmoidOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["hardsigmoid"]}


@PT_OPERATOR_METATYPES.register()
class PTTanhMetatype(PTOperatorMetatype):
    name = "TanhOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["tanh"], NamespaceTarget.TORCH: ["tanh"]}


@PT_OPERATOR_METATYPES.register()
class PTELUMetatype(PTOperatorMetatype):
    name = "EluOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["elu", "elu_"]}


@PT_OPERATOR_METATYPES.register()
class PTPRELUMetatype(PTOperatorMetatype):
    name = "PReluOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["prelu"]}


@PT_OPERATOR_METATYPES.register()
class PTLeakyRELUMetatype(PTOperatorMetatype):
    name = "LeakyReluOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["leaky_relu"]}


@PT_OPERATOR_METATYPES.register()
class PTModuleLayerNormMetatype(PTModuleOperatorSubtype):
    name = "LayerNormOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["layer_norm"]}
    hw_config_names = [HWConfigOpName.MVN]


@PT_OPERATOR_METATYPES.register()
class PTLayerNormMetatype(PTOperatorMetatype):
    name = "LayerNormOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["layer_norm"]}
    hw_config_names = [HWConfigOpName.MVN]
    subtypes = [PTModuleLayerNormMetatype]


@PT_OPERATOR_METATYPES.register()
class PTModuleGroupNormMetatype(PTModuleOperatorSubtype):
    name = "GroupNormOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["group_norm"]}
    hw_config_names = [HWConfigOpName.MVN]


@PT_OPERATOR_METATYPES.register()
class PTGroupNormMetatype(PTOperatorMetatype):
    name = "GroupNormOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["group_norm"]}
    hw_config_names = [HWConfigOpName.MVN]
    subtypes = [PTModuleGroupNormMetatype]


@PT_OPERATOR_METATYPES.register()
class PTGELUMetatype(PTOperatorMetatype):
    name = "GeluOp"
    hw_config_names = [HWConfigOpName.GELU]
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["gelu"]}


@PT_OPERATOR_METATYPES.register()
class PTSILUMetatype(PTOperatorMetatype):
    name = "SiluOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["silu"]}


@PT_OPERATOR_METATYPES.register()
class PTSigmoidMetatype(PTOperatorMetatype):
    name = "SigmoidOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_NN_FUNCTIONAL: ["sigmoid"],
        NamespaceTarget.TORCH_TENSOR: ["sigmoid"],
        NamespaceTarget.TORCH: ["sigmoid"],
    }


@PT_OPERATOR_METATYPES.register()
class PTAddMetatype(PTOperatorMetatype):
    name = "AddOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["add", "__add__", "__iadd__", "__radd__"],
        NamespaceTarget.TORCH: ["add"],
    }
    hw_config_names = [HWConfigOpName.ADD]


@PT_OPERATOR_METATYPES.register()
class PTSubMetatype(PTOperatorMetatype):
    name = "SubOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["sub", "__sub__", "__isub__", "__rsub__"],
        NamespaceTarget.TORCH: ["sub"],
    }
    hw_config_names = [HWConfigOpName.SUBTRACT]


@PT_OPERATOR_METATYPES.register()
class PTMulMetatype(PTOperatorMetatype):
    name = "MulOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["mul", "__mul__", "__imul__", "__rmul__"],
        NamespaceTarget.TORCH: ["mul"],
    }
    hw_config_names = [HWConfigOpName.MULTIPLY]


@PT_OPERATOR_METATYPES.register()
class PTDivMetatype(PTOperatorMetatype):
    name = "DivOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["__div__", "__idiv__", "__truediv__"],
        NamespaceTarget.TORCH: ["div"],
    }
    hw_config_names = [HWConfigOpName.DIVIDE]


@PT_OPERATOR_METATYPES.register()
class PTFloorDivMetatype(PTOperatorMetatype):
    name = "FloordivOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["__floordiv__", "__ifloordiv__", "__rfloordiv__"],
    }


@PT_OPERATOR_METATYPES.register()
class PTExpMetatype(PTOperatorMetatype):
    name = "ExpOp"
    module_to_function_names = {
        NamespaceTarget.TORCH: ["exp"],
    }


@PT_OPERATOR_METATYPES.register()
class PTLogMetatype(PTOperatorMetatype):
    name = "LogOp"
    module_to_function_names = {
        NamespaceTarget.TORCH: ["log"],
    }


@PT_OPERATOR_METATYPES.register()
class PTAbsMetatype(PTOperatorMetatype):
    name = "AbsOp"
    module_to_function_names = {
        NamespaceTarget.TORCH: ["abs"],
    }


@PT_OPERATOR_METATYPES.register()
class PTErfMetatype(PTOperatorMetatype):
    name = "ErfOp"
    module_to_function_names = {
        NamespaceTarget.TORCH: ["erf"],
    }


@PT_OPERATOR_METATYPES.register()
class PTMatMulMetatype(PTOperatorMetatype):
    name = "MatMulOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["matmul", "__matmul__"],
        NamespaceTarget.TORCH: ["matmul", "bmm", "mm"],
    }
    hw_config_names = [HWConfigOpName.MATMUL]


@PT_OPERATOR_METATYPES.register()
class PTBaddBmmMetatype(PTOperatorMetatype):
    name = "MatMulOp"
    module_to_function_names = {NamespaceTarget.TORCH: ["baddbmm"]}
    hw_config_names = [HWConfigOpName.MATMUL]
    # 0-th arg to the baddbmm is basically a (b)ias to be (add)ed to the (bmm) operation,
    # presuming that most runtime implementations will fuse the bias addition into the matrix multiplication
    # and therefore won't quantize the bias input, as this would break the hardware-fused pattern.
    ignored_input_ports: List[int] = [0]


@PT_OPERATOR_METATYPES.register()
class PTMeanMetatype(PTOperatorMetatype):
    name = "MeanOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["mean"]}
    hw_config_names = [HWConfigOpName.REDUCEMEAN]


@PT_OPERATOR_METATYPES.register()
class PTRoundMetatype(PTOperatorMetatype):
    name = "RoundOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["round"]}


@PT_OPERATOR_METATYPES.register()
class PTDropoutMetatype(PTOperatorMetatype):
    name = "DropoutOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["dropout"]}


@PT_OPERATOR_METATYPES.register()
class PTThresholdMetatype(PTOperatorMetatype):
    name = "ThresholdOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["threshold"]}


@PT_OPERATOR_METATYPES.register()
class PTModuleBatchNormMetatype(PTModuleOperatorSubtype):
    name = "BatchNormOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["batch_norm"]}


@PT_OPERATOR_METATYPES.register()
class PTBatchNormMetatype(PTOperatorMetatype):
    name = "BatchNormOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["batch_norm"]}
    subtypes = [PTModuleBatchNormMetatype]


@PT_OPERATOR_METATYPES.register()
class PTAvgPool2dMetatype(PTOperatorMetatype):
    name = "AvgPool2DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["avg_pool2d", "adaptive_avg_pool2d"]}
    hw_config_names = [HWConfigOpName.AVGPOOL]


@PT_OPERATOR_METATYPES.register()
class PTAvgPool3dMetatype(PTOperatorMetatype):
    name = "AvgPool3DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["avg_pool3d", "adaptive_avg_pool3d"]}
    hw_config_names = [HWConfigOpName.AVGPOOL]


class PTMaxPool1dMetatype(PTOperatorMetatype):
    name = "MaxPool1DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["max_pool1d", "adaptive_max_pool1d"]}
    hw_config_names = [HWConfigOpName.MAXPOOL]


@PT_OPERATOR_METATYPES.register()
class PTMaxPool2dMetatype(PTOperatorMetatype):
    name = "MaxPool2DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["max_pool2d", "adaptive_max_pool2d"]}
    hw_config_names = [HWConfigOpName.MAXPOOL]


@PT_OPERATOR_METATYPES.register()
class PTMaxPool3dMetatype(PTOperatorMetatype):
    name = "MaxPool3DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["max_pool3d", "adaptive_max_pool3d"]}
    hw_config_names = [HWConfigOpName.MAXPOOL]


@PT_OPERATOR_METATYPES.register()
class PTMaxUnpool1dMetatype(PTOperatorMetatype):
    name = "MaxUnPool1DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["max_unpool1d"]}


@PT_OPERATOR_METATYPES.register()
class PTMaxUnpool2dMetatype(PTOperatorMetatype):
    name = "MaxUnPool2DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["max_unpool2d"]}


@PT_OPERATOR_METATYPES.register()
class PTMaxUnpool3dMetatype(PTOperatorMetatype):
    name = "MaxUnPool3DOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["max_unpool3d"]}


@PT_OPERATOR_METATYPES.register()
class PTPadMetatype(PTOperatorMetatype):
    name = "PadOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["pad"]}


@PT_OPERATOR_METATYPES.register()
class PTCatMetatype(PTOperatorMetatype):
    name = "CatOp"
    module_to_function_names = {NamespaceTarget.TORCH: ["cat", "stack"]}
    hw_config_names = [HWConfigOpName.CONCAT]


@PT_OPERATOR_METATYPES.register()
class PTRELUMetatype(PTOperatorMetatype):
    name = "ReluOp"
    module_to_function_names = {NamespaceTarget.TORCH: ["relu", "relu_"]}


@PT_OPERATOR_METATYPES.register()
class PTRELU6Metatype(PTOperatorMetatype):
    name = "Relu6Op"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["relu6"]}


@PT_OPERATOR_METATYPES.register()
class PTMaxMetatype(PTOperatorMetatype):
    name = "MaxOp"
    module_to_function_names = {NamespaceTarget.TORCH: ["max"]}
    hw_config_names = [HWConfigOpName.MAXIMUM, HWConfigOpName.REDUCEMAX]


@PT_OPERATOR_METATYPES.register()
class PTMinMetatype(PTOperatorMetatype):
    name = "MinOp"
    module_to_function_names = {NamespaceTarget.TORCH: ["min"]}
    hw_config_names = [HWConfigOpName.MINIMUM]


@PT_OPERATOR_METATYPES.register()
class PTTransposeMetatype(PTOperatorMetatype):
    name = "TransposeOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["transpose", "permute", "transpose_"],
        NamespaceTarget.TORCH: ["transpose"],
    }
    hw_config_names = [HWConfigOpName.TRANSPOSE]


@PT_OPERATOR_METATYPES.register()
class PTGatherMetatype(PTOperatorMetatype):
    name = "GatherOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["index_select", "__getitem__"],
        NamespaceTarget.TORCH: ["gather", "index_select", "where"],
    }


@PT_OPERATOR_METATYPES.register()
class PTScatterMetatype(PTOperatorMetatype):
    name = "ScatterOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["scatter", "masked_fill", "masked_fill_"]}


@PT_OPERATOR_METATYPES.register()
class PTReshapeMetatype(PTOperatorMetatype):
    name = "ReshapeOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["reshape", "view", "flatten", "unsqueeze"],
        NamespaceTarget.TORCH: ["flatten", "unsqueeze"],
    }
    hw_config_names = [HWConfigOpName.RESHAPE, HWConfigOpName.UNSQUEEZE, HWConfigOpName.FLATTEN]


@PT_OPERATOR_METATYPES.register()
class PTSqueezeMetatype(PTOperatorMetatype):
    name = "SqueezeOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["squeeze"],
        NamespaceTarget.TORCH: ["squeeze"],
    }
    hw_config_names = [HWConfigOpName.SQUEEZE]


@PT_OPERATOR_METATYPES.register()
class PTSplitMetatype(PTOperatorMetatype):
    name = "SplitOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["split", "chunk", "unbind"]}
    hw_config_names = [HWConfigOpName.SPLIT, HWConfigOpName.CHUNK]


@PT_OPERATOR_METATYPES.register()
class PTExpandMetatype(PTOperatorMetatype):
    name = "ExpandOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["expand"]}


@PT_OPERATOR_METATYPES.register()
class PTExpandAsMetatype(PTOperatorMetatype):
    name = "ExpandAsOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["expand_as"]}


# Non-quantizable ops
@PT_OPERATOR_METATYPES.register()
class PTModuleEmbeddingMetatype(PTModuleOperatorSubtype):
    name = "EmbeddingOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["embedding"]}
    hw_config_names = [HWConfigOpName.EMBEDDING]


@PT_OPERATOR_METATYPES.register()
class PTEmbeddingMetatype(PTOperatorMetatype):
    name = "EmbeddingOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["embedding"]}
    hw_config_names = [HWConfigOpName.EMBEDDING]
    subtypes = [PTModuleEmbeddingMetatype]


@PT_OPERATOR_METATYPES.register()
class PTModuleEmbeddingBagMetatype(PTModuleOperatorSubtype):
    name = "EmbeddingBagOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["embedding_bag"]}
    hw_config_names = [HWConfigOpName.EMBEDDINGBAG]


@PT_OPERATOR_METATYPES.register()
class PTEmbeddingBagMetatype(PTOperatorMetatype):
    name = "EmbeddingBagOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["embedding_bag"]}
    hw_config_names = [HWConfigOpName.EMBEDDINGBAG]
    subtypes = [PTModuleEmbeddingBagMetatype]


@PT_OPERATOR_METATYPES.register()
class PTSoftmaxMetatype(PTOperatorMetatype):
    name = "SoftmaxOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["softmax"]}


@PT_OPERATOR_METATYPES.register()
class PTLessMetatype(PTOperatorMetatype):
    name = "LessOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__lt__"]}
    hw_config_names = [HWConfigOpName.LESS]


@PT_OPERATOR_METATYPES.register()
class PTLessEqualMetatype(PTOperatorMetatype):
    name = "LessEqualOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__le__"]}
    hw_config_names = [HWConfigOpName.LESSEQUAL]


@PT_OPERATOR_METATYPES.register()
class PTGreaterMetatype(PTOperatorMetatype):
    name = "GreaterOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__gt__"]}
    hw_config_names = [HWConfigOpName.GREATER]


@PT_OPERATOR_METATYPES.register()
class PTGreaterEqualMetatype(PTOperatorMetatype):
    name = "GreaterEqualOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__ge__"]}
    hw_config_names = [HWConfigOpName.GREATEREQUAL]


@PT_OPERATOR_METATYPES.register()
class PTModMetatype(PTOperatorMetatype):
    name = "ModOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__mod__"]}
    hw_config_names = [HWConfigOpName.FLOORMOD]


@PT_OPERATOR_METATYPES.register()
class PTEqualsMetatype(PTOperatorMetatype):
    name = "EqualsOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__eq__"]}
    hw_config_names = [HWConfigOpName.EQUAL]


@PT_OPERATOR_METATYPES.register()
class PTNotEqualMetatype(PTOperatorMetatype):
    name = "NotEqualOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__ne__"]}
    hw_config_names = [HWConfigOpName.NOTEQUAL]


@PT_OPERATOR_METATYPES.register()
class PTLogicalOrMetatype(PTOperatorMetatype):
    name = "LogicalOrOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__or__"]}
    hw_config_names = [HWConfigOpName.LOGICALOR]


@PT_OPERATOR_METATYPES.register()
class PTLogicalXorMetatype(PTOperatorMetatype):
    name = "LogicalXorOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__xor__"]}
    hw_config_names = [HWConfigOpName.LOGICALXOR]


@PT_OPERATOR_METATYPES.register()
class PTLogicalAndMetatype(PTOperatorMetatype):
    name = "LogicalAndOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__and__"]}
    hw_config_names = [HWConfigOpName.LOGICALAND]


@PT_OPERATOR_METATYPES.register()
class PTLogicalNotMetatype(PTOperatorMetatype):
    name = "LogicalNotOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["logical_not_"]}
    hw_config_names = [HWConfigOpName.LOGICALNOT]


@PT_OPERATOR_METATYPES.register()
class PTPowerMetatype(PTOperatorMetatype):
    name = "PowerOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["__pow__", "pow"], NamespaceTarget.TORCH: ["pow"]}
    hw_config_names = [HWConfigOpName.POWER]


@PT_OPERATOR_METATYPES.register()
class PTSqrtMetatype(PTOperatorMetatype):
    name = "SqrtOp"
    module_to_function_names = {
        NamespaceTarget.TORCH_TENSOR: ["sqrt", "sqrt_"],
        NamespaceTarget.TORCH: ["sqrt", "sqrt_"],
    }
    hw_config_names = [HWConfigOpName.POWER]


@PT_OPERATOR_METATYPES.register()
class PTInterpolateMetatype(PTOperatorMetatype):
    name = "InterpolateOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["interpolate"]}
    hw_config_names = [HWConfigOpName.INTERPOLATE]


@PT_OPERATOR_METATYPES.register()
class PTRepeatMetatype(PTOperatorMetatype):
    name = "RepeatOp"
    module_to_function_names = {NamespaceTarget.TORCH: ["repeat_interleave"]}
    hw_config_names = [HWConfigOpName.TILE]


@PT_OPERATOR_METATYPES.register()
class PTPixelShuffleMetatype(PTOperatorMetatype):
    name = "PixelShuffleOp"
    module_to_function_names = {NamespaceTarget.TORCH_NN_FUNCTIONAL: ["pixel_shuffle"]}


@PT_OPERATOR_METATYPES.register()
class PTSumMetatype(PTOperatorMetatype):
    name = "SumOp"
    module_to_function_names = {NamespaceTarget.TORCH_TENSOR: ["sum"], NamespaceTarget.TORCH: ["sum"]}
    hw_config_names = [HWConfigOpName.REDUCESUM]


@PT_OPERATOR_METATYPES.register()
class PTReduceL2(PTOperatorMetatype):
    name = "ReduceL2"
    module_to_function_names = {
        NamespaceTarget.TORCH_NN_FUNCTIONAL: ["normalize"],  # note: normalize is for general L_p normalization
    }
    hw_config_names = [HWConfigOpName.REDUCEL2]


def get_operator_metatypes() -> List[Type[OperatorMetatype]]:
    """
    Returns a list of the operator metatypes.

    :return: List of operator metatypes .
    """
    return list(PT_OPERATOR_METATYPES.registry_dict.values())


OPERATORS_WITH_WEIGHTS_METATYPES = [
    PTModuleConv1dMetatype,
    PTModuleConv2dMetatype,
    PTModuleConv3dMetatype,
    PTDepthwiseConv1dSubtype,
    PTDepthwiseConv2dSubtype,
    PTDepthwiseConv3dSubtype,
    PTModuleLinearMetatype,
    PTModuleBatchNormMetatype,
    PTModuleGroupNormMetatype,
    PTModuleLayerNormMetatype,
    PTModuleConvTranspose1dMetatype,
    PTModuleConvTranspose2dMetatype,
    PTModuleConvTranspose3dMetatype,
    PTModuleEmbeddingMetatype,
    PTModuleEmbeddingBagMetatype,
]

UNIFICATION_PRODUCING_METATYPES = [
    PTModuleConv1dMetatype,
    PTModuleConv2dMetatype,
    PTModuleConv3dMetatype,
    PTDepthwiseConv1dSubtype,
    PTDepthwiseConv2dSubtype,
    PTDepthwiseConv3dSubtype,
    PTModuleConvTranspose1dMetatype,
    PTModuleConvTranspose2dMetatype,
    PTModuleConvTranspose3dMetatype,
    PTModuleLinearMetatype,
]

OP_NAMES_WITH_WEIGHTS = [x for meta in OPERATORS_WITH_WEIGHTS_METATYPES for x in meta.get_all_aliases()]

# Contains the operation metatypes for which bias can be applied.
OPERATORS_WITH_BIAS_METATYPES = [
    PTModuleConv1dMetatype,
    PTModuleConv2dMetatype,
    PTModuleConv3dMetatype,
    PTDepthwiseConv1dSubtype,
    PTDepthwiseConv2dSubtype,
    PTDepthwiseConv3dSubtype,
    PTModuleConvTranspose1dMetatype,
    PTModuleConvTranspose2dMetatype,
    PTModuleConvTranspose3dMetatype,
]

OPERATORS_FUSED_METATYPES = [
    PTModuleBatchNormMetatype,
]

OP_NAMES_QUANTIZE_NODE = ["symmetric_quantize", "asymmetric_quantize"]
