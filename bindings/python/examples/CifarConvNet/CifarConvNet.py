﻿# ==============================================================================
# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

import os
import math
import numpy as np
from cntk.blocks import *  # non-layer like building blocks such as LSTM()
from cntk.layers import *  # layer-like stuff
from cntk.models import *  # higher abstraction level, e.g. entire standard models and also operators like Sequential()
from cntk.utils import *
from cntk.io import MinibatchSource, ImageDeserializer, StreamDef, StreamDefs
from cntk.initializer import glorot_uniform
from cntk import Trainer
from cntk.ops import cross_entropy_with_softmax, classification_error, relu, convolution, pooling, PoolingType_Max
from cntk.learner import momentum_sgd, learning_rate_schedule
from cntk.persist import load_model, save_model


########################
# variables and paths  #
########################

# paths (are relative to current python file)
abs_path   = os.path.dirname(os.path.abspath(__file__))
cntk_path  = os.path.normpath(os.path.join(abs_path, "..", "..", "..", ".."))
data_path  = os.path.join(cntk_path, "Examples", "Image", "Datasets", "CIFAR-10")
model_path = os.path.join(abs_path, "Models")

# model dimensions
image_height = 32
image_width  = 32
num_channels = 3  # RGB
num_classes  = 10

#
# Define the reader for both training and evaluation action.
#
def create_reader(map_file, mean_file, train):
    if not os.path.exists(map_file) or not os.path.exists(mean_file):
        cifar_py3 = "" if sys.version_info.major < 3 else "_py3"
        raise RuntimeError("File '%s' or '%s' does not exist. Please run CifarDownload%s.py and CifarConverter%s.py from CIFAR-10 to fetch them" %
                           (map_file, mean_file, cifar_py3, cifar_py3))

    # transformation pipeline for the features has jitter/crop only when training
    transforms = []
    if train:
        transforms += [
            ImageDeserializer.crop(crop_type='Random', ratio=0.8, jitter_type='uniRatio') # train uses jitter
        ]
    transforms += [
        ImageDeserializer.scale(width=image_width, height=image_height, channels=num_channels, interpolations='linear'),
        ImageDeserializer.mean(mean_file)
    ]
    # deserializer
    return MinibatchSource(ImageDeserializer(map_file, StreamDefs(
        features = StreamDef(field='image', transforms=transforms), # first column in map file is referred to as 'image'
        labels   = StreamDef(field='label', shape=num_classes)      # and second as 'label'
    )))

#
# Define a VGG like network for Cifar dataset.
#
#       | VGG9          |
#       | ------------- |
#       | conv3-64      |
#       | conv3-64      |
#       | max3          |
#       |               |
#       | conv3-96      |
#       | conv3-96      |
#       | max3          |
#       |               |
#       | conv3-128     |
#       | conv3-128     |
#       | max3          |
#       |               |
#       | FC-1024       |
#       | dropout0.5    |
#       |               |
#       | FC-1024       |
#       | dropout0.5    |
#       |               |
#       | FC-10         |
#
# TODO: remove 'input'
def create_vgg9_model(input, num_classes):
    with default_options(activation=relu):
        model = Sequential([
            For(range(3), lambda i: [
                Convolution((3,3), [64,96,128][i], pad=True),
                Convolution((3,3), [64,96,128][i], pad=True),
                MaxPooling((3,3), strides=(2,2))
            ]),
            For(range(2), lambda : [
                Dense(1024),
                Dropout(0.5)
            ]),
            Dense(num_classes, activation=None)
        ])

    return model(input)

#
# Train and evaluate the network.
#
def train_and_evaluate(reader_train, reader_test, max_epochs):

    # Input variables denoting the features and label data
    input_var = input_variable((num_channels, image_height, image_width))
    label_var = input_variable((num_classes))

    # apply model to input
    z = create_vgg9_model(input_var, 10)

    #
    # Training action
    #

    # loss and metric
    ce = cross_entropy_with_softmax(z, label_var)
    pe = classification_error(z, label_var)

    # training config
    epoch_size     = 50000
    minibatch_size = 64
    #epoch_size = 1000 ; max_epochs = 1 # for faster testing

    # For basic model
    lr_per_sample       = [0.00015625]*10+[0.000046875]*10+[0.0000156]
    momentum_per_sample = 0.9 ** (1.0 / minibatch_size)  # BUGBUG: why does this work? Should be as time const, no?
    l2_reg_weight       = 0.03

    # trainer object
    lr_schedule = learning_rate_schedule(lr_per_sample, units=epoch_size)
    learner     = momentum_sgd(z.parameters, lr_schedule, momentum_per_sample, 
                               l2_regularization_weight = l2_reg_weight)
    trainer     = Trainer(z, ce, pe, learner)

    # define mapping from reader streams to network inputs
    input_map = {
        input_var: reader_train.streams.features,
        label_var: reader_train.streams.labels
    }

    log_number_of_parameters(z) ; print()
    progress_printer = ProgressPrinter(tag='Training')

    # perform model training
    for epoch in range(max_epochs):       # loop over epochs
        sample_count = 0
        while sample_count < epoch_size:  # loop over minibatches in the epoch
            data = reader_train.next_minibatch(min(minibatch_size, epoch_size - sample_count), input_map=input_map) # fetch minibatch.
            trainer.train_minibatch(data)                                   # update model with it

            sample_count += data[label_var].num_samples                     # count samples processed so far
            progress_printer.update_with_trainer(trainer, with_metric=True) # log progress
        loss, metric, actual_samples = progress_printer.epoch_summary(with_metric=True)

    #
    # Evaluation action
    #
    epoch_size     = 10000
    minibatch_size = 16

    # process minibatches and evaluate the model
    metric_numer    = 0
    metric_denom    = 0
    sample_count    = 0
    minibatch_index = 0

    #progress_printer = ProgressPrinter(freq=100, first=10, tag='Eval')
    while sample_count < epoch_size:
        current_minibatch = min(minibatch_size, epoch_size - sample_count)

        # Fetch next test min batch.
        data = reader_test.next_minibatch(current_minibatch, input_map=input_map)

        # minibatch data to be trained with
        metric_numer += trainer.test_minibatch(data) * current_minibatch
        metric_denom += current_minibatch

        # Keep track of the number of samples processed so far.
        sample_count += data[label_var].num_samples
        minibatch_index += 1

    print("")
    print("Final Results: Minibatch[1-{}]: errs = {:0.1f}% * {}".format(minibatch_index+1, (metric_numer*100.0)/metric_denom, metric_denom))
    print("")

    return loss, metric # return values from last epoch

########################
# eval action          #
########################

def evaluate(reader, model):
    # Input variables denoting the features and label data
    input_var = input_variable((num_channels, image_height, image_width))
    label_var = input_variable((num_classes))

    # apply model to input
    #z = model(input_var)
    input_var = model.arguments[0]  # workaround
    z = model
    # BUGBUG: still fails eval with "RuntimeError: __v2libuid__BatchNormalization456__v2libname__BatchNormalization11: inference mode is used, but nothing has been trained."

    # loss and metric
    ce = cross_entropy_with_softmax(z, label_var)
    pe = classification_error      (z, label_var)

    # define mapping from reader streams to network inputs
    input_map = {
        input_var: reader.streams.features,
        label_var: reader.streams.labels
    }

    # process minibatches and perform evaluation
    dummy_learner = momentum_sgd(z.parameters, 1, 0) # BUGBUG: should not be needed
    evaluator = Trainer(z, ce, pe, [dummy_learner])
    progress_printer = ProgressPrinter(freq=100, first=10, tag='Evaluation') # more detailed logging
    #progress_printer = ProgressPrinter(tag='Evaluation')

    while True:
        minibatch_size = 1000
        data = reader.next_minibatch(minibatch_size, input_map=input_map) # fetch minibatch
        if not data:                                                      # until we hit the end
            break
        metric = evaluator.test_minibatch(data)                           # evaluate minibatch
        progress_printer.update(0, data[slot_labels].num_samples, metric) # log progress
    loss, metric, actual_samples = progress_printer.epoch_summary(with_metric=True)

    return loss, metric

#############################
# main function boilerplate #
#############################

if __name__=='__main__':
    # TODO: leave these in for now as debugging aids; remove for beta
    from _cntk_py import set_computation_network_trace_level, set_fixed_random_seed, force_deterministic_algorithms
    set_computation_network_trace_level(1)  # TODO: remove debugging facilities once this all works
    set_fixed_random_seed(1)  # BUGBUG: has no effect at present  # TODO: remove debugging facilities once this all works
    #force_deterministic_algorithms()
    # TODO: do the above; they lead to slightly different results, so not doing it for now

    reader_train = create_reader(os.path.join(data_path, 'train_map.txt'), os.path.join(data_path, 'CIFAR-10_mean.xml'), True)
    reader_test  = create_reader(os.path.join(data_path, 'test_map.txt'), os.path.join(data_path, 'CIFAR-10_mean.xml'), False)
    # create model

    #model = create_basic_model_layer()   # TODO: clean this up more

    # train
    #reader_train = create_reader(data_path, 'train_map.txt', 'CIFAR-10_mean.xml', is_training=True)
    #reader_test  = create_reader(data_path, 'test_map.txt',  'CIFAR-10_mean.xml', is_training=False)
    #train_and_evaluate(reader_train, reader_test, model, max_epochs=10)

    # save and load (as an illustration)
    #path = data_path + "/model.cmf"
    #save_model(model, path)
    #model = load_model(path)

    # test
    reader_test  = create_reader(data_path, 'test_map.txt', 'CIFAR-10_mean.xml', is_training=False)
    # BUGBUG: fails with "RuntimeError: __v2libuid__BatchNormalization430__v2libname__BatchNormalization19: inference mode is used, but nothing has been trained."
    #evaluate(reader_test, model)
