from queue import Queue
import kerastuner
import numpy as np
import tensorflow as tf
from tensorflow.python.util import nest

from autokeras.hypermodel import hyper_block
from autokeras.hypermodel import hyper_head
from autokeras import layer_utils
from autokeras import const


class GraphAutoModel(kerastuner.HyperModel):
    """A HyperModel defined by a graph of HyperBlocks.

    GraphAutoModel is a subclass of HyperModel. Besides the HyperModel properties,
    it also has a tuner to tune the HyperModel. The user can use it in a similar
    way to a Keras model since it also has `fit()` and  `predict()` methods.

    The user can specify the high-level neural architecture by connecting the
    HyperBlocks with the functional API, which is the same as
    the Keras functional API.

    Attributes:
        inputs: A list of or a HyperNode instances.
            The input node(s) of the GraphAutoModel.
        outputs: A list of or a HyperNode instances.
            The output node(s) of the GraphAutoModel.
        max_trials: Int. The maximum number of different models to try.
        directory: String. The path to the directory
            for storing the search outputs.
    """

    def __init__(self,
                 inputs,
                 outputs,
                 max_trials=None,
                 directory=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.inputs = layer_utils.format_inputs(inputs)
        self.outputs = layer_utils.format_inputs(outputs)
        self.tuner = None
        self.max_trials = max_trials or const.Constant.NUM_TRAILS
        self.directory = directory or const.Constant.TEMP_DIRECTORY
        self._node_to_id = {}
        self._nodes = []
        self._hypermodels = []
        self._hypermodel_to_id = {}
        self._build_network()

    def build(self, hp):
        real_nodes = {}
        for input_node in self.inputs:
            node_id = self._node_to_id[input_node]
            real_nodes[node_id] = input_node.build(hp)
        for hypermodel in self._hypermodels:
            temp_inputs = [real_nodes[self._node_to_id[input_node]]
                           for input_node in hypermodel.inputs]
            outputs = hypermodel.build(hp,
                                       inputs=temp_inputs)
            outputs = layer_utils.format_inputs(outputs, hypermodel.name)
            for output_node, real_output_node in zip(hypermodel.outputs,
                                                     outputs):
                real_nodes[self._node_to_id[output_node]] = real_output_node
        model = tf.keras.Model(
            [real_nodes[self._node_to_id[input_node]] for input_node in
             self.inputs],
            [real_nodes[self._node_to_id[output_node]] for output_node in
             self.outputs])

        return self._compiled(hp, model)

    def _build_network(self):
        self._node_to_id = {}

        # Recursively find all the interested nodes.
        for input_node in self.inputs:
            self._search_network(input_node, self.outputs, set(), set())
        self._nodes = sorted(list(self._node_to_id.keys()),
                             key=lambda x: self._node_to_id[x])

        for node in (self.inputs + self.outputs):
            if node not in self._node_to_id:
                raise ValueError('Inputs and outputs not connected.')

        # Find the hypermodels and sort the hypermodels in topological order.
        self._hypermodels = []
        self._hypermodel_to_id = {}
        visited_nodes = set()
        queue = Queue()
        for input_node in self.inputs:
            queue.put(input_node)
            visited_nodes.add(input_node)
        while not queue.empty():
            input_node = queue.get()
            for hypermodel in input_node.out_hypermodels:
                # Check at least one output node of the hypermodel is in the
                # interested nodes.
                if not any([output_node in self._node_to_id for output_node in
                            hypermodel.outputs]):
                    continue
                self._add_hypermodel(hypermodel)
                for output_node in hypermodel.outputs:
                    # The node is not visited and in interested nodes.
                    if (output_node not in visited_nodes and
                            output_node in self._node_to_id):
                        visited_nodes.add(output_node)
                        queue.put(output_node)
        for output_node in self.outputs:
            hypermodel = output_node.in_hypermodels[0]
            hypermodel.output_shape = output_node.shape

    def _search_network(self, input_node, outputs, in_stack_nodes,
                        visited_nodes):
        visited_nodes.add(input_node)
        in_stack_nodes.add(input_node)

        outputs_reached = False
        if input_node in outputs:
            outputs_reached = True

        for hypermodel in input_node.out_hypermodels:
            for output_node in hypermodel.outputs:
                if output_node in in_stack_nodes:
                    raise ValueError('The network has a cycle.')
                if output_node not in visited_nodes:
                    self._search_network(output_node, outputs, in_stack_nodes,
                                         visited_nodes)
                if output_node in self._node_to_id.keys():
                    outputs_reached = True

        if outputs_reached:
            self._add_node(input_node)

        in_stack_nodes.remove(input_node)

    def _add_hypermodel(self, hypermodel):
        if hypermodel not in self._hypermodels:
            hypermodel_id = len(self._hypermodels)
            self._hypermodel_to_id[hypermodel] = hypermodel_id
            self._hypermodels.append(hypermodel)
        for output_node in hypermodel.outputs:
            self._add_node(output_node)
        for input_node in hypermodel.inputs:
            if input_node not in self._node_to_id:
                raise ValueError(
                    'A required input is missing '
                    'for HyperModel {name}.'.format(
                        name=hypermodel.name))

    def _add_node(self, input_node):
        if input_node not in self._node_to_id:
            self._node_to_id[input_node] = len(self._node_to_id)

    def fit(self,
            x=None,
            y=None,
            validation_data=None,
            **kwargs):
        # Initialize HyperGraph model
        x = layer_utils.format_inputs(x, 'train_x')
        y = layer_utils.format_inputs(y, 'train_y')

        # TODO: Set the shapes only if they are not provided by the user when
        #  initiating the HyperHead or Block.
        for x_input, input_node in zip(x, self.inputs):
            input_node.shape = x_input.shape[1:]
        for y_input, output_node in zip(y, self.outputs):
            if len(y_input.shape) == 1:
                y_input = np.reshape(y_input, y_input.shape + (1,))
            output_node.shape = y_input.shape[1:]
            output_node.in_hypermodels[0].output_shape = output_node.shape

        # Prepare the dataset
        if validation_data is None:
            (x, y), (x_val, y_val) = layer_utils.split_train_to_valid(x, y)
            validation_data = x_val, y_val

        self.tuner = kerastuner.RandomSearch(
            hypermodel=self,
            objective='val_loss',
            max_trials=self.max_trials,
            directory=self.directory)

        # TODO: allow early stop if epochs is not specified.
        self.tuner.search(x=x,
                          y=y,
                          validation_data=validation_data,
                          **kwargs)

    def predict(self, x, **kwargs):
        """Predict the output for a given testing data. """
        return self.tuner.get_best_models(1)[0].predict(x, **kwargs)

    def _get_metrics(self):
        metrics = []
        for metrics_list in [output_node.in_hypermodels[0].metrics for
                             output_node in self.outputs
                             if isinstance(output_node.in_hypermodels[0],
                                           hyper_head.HyperHead)]:
            metrics += metrics_list
        return metrics

    def _get_loss(self):
        loss = nest.flatten([output_node.in_hypermodels[0].loss
                             for output_node in self.outputs
                             if isinstance(output_node.in_hypermodels[0],
                                           hyper_head.HyperHead)])
        return loss

    def _compiled(self, hp, model):
        # Specify hyperparameters from compile(...)
        optimizer = hp.Choice('optimizer',
                              ['adam',
                               'adadelta',
                               'sgd'])

        model.compile(optimizer=optimizer,
                      metrics=self._get_metrics(),
                      loss=self._get_loss())

        return model


class AutoModel(GraphAutoModel):
    """ A HyperModel defined by inputs and outputs.

    AutoModel is a subclass of HyperModel. Besides the HyperModel properties,
    it also has a tuner to tune the HyperModel. The user can use it in a similar
    way to a Keras model since it also has `fit()` and  `predict()` methods.

    The user can specify the inputs and outputs of the AutoModel. It will infer
    the rest of the high-level neural architecture.

    Attributes:
        inputs: A list of or a HyperNode instance.
            The input node(s) of a the AutoModel.
        outputs: A list of or a HyperHead instance.
            The output head(s) of the AutoModel.
        max_trials: Int. The maximum number of different models to try.
        directory: String. The path to a directory for storing the search outputs.
    """

    def __init__(self,
                 inputs,
                 outputs,
                 **kwargs):
        inputs = layer_utils.format_inputs(inputs)
        outputs = layer_utils.format_inputs(outputs)
        middle_nodes = [input_node.related_block()(input_node)
                        for input_node in inputs]
        if len(middle_nodes) > 1:
            output_node = hyper_block.Merge()(middle_nodes)
        else:
            output_node = middle_nodes[0]
        outputs = [output_blocks(output_node)
                   for output_blocks in outputs]
        super().__init__(inputs, outputs, **kwargs)
