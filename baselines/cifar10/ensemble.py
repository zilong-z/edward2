# coding=utf-8
# Copyright 2020 The Edward2 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Ensemble on CIFAR.

This script only performs evaluation, not training. We recommend training
ensembles by launching independent runs of `deterministic.py` over different
seeds. Set `output_dir` to the directory containing these checkpoints.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time

from absl import app
from absl import flags
from absl import logging

import deterministic  # local file import
import utils  # local file import

import tensorflow.compat.v2 as tf
import tensorflow_datasets as tfds

# TODO(trandustin): We inherit
# FLAGS.{dataset,per_core_batch_size,output_dir,seed} from deterministic. This
# is not intuitive, which suggests we need to either refactor to avoid importing
# from a binary or duplicate the model definition here.
flags.mark_flag_as_required('output_dir')
FLAGS = flags.FLAGS


def ensemble_negative_log_likelihood(labels, logits):
  """Negative log-likelihood for ensemble.

  For each datapoint (x,y), the ensemble's negative log-likelihood is:

  ```
  -log p(y|x) = -log sum_{m=1}^{ensemble_size} exp(log p(y|x,theta_m)) +
                log ensemble_size.
  ```

  Args:
    labels: tf.Tensor of shape [...].
    logits: tf.Tensor of shape [ensemble_size, ..., num_classes].

  Returns:
    tf.Tensor of shape [...].
  """
  labels = tf.cast(labels, tf.int32)
  logits = tf.convert_to_tensor(logits)
  ensemble_size = float(logits.shape[0])
  nll = tf.nn.sparse_softmax_cross_entropy_with_logits(
      tf.broadcast_to(labels[tf.newaxis, ...], tf.shape(logits)[:-1]),
      logits)
  return -tf.reduce_logsumexp(-nll, axis=0) + tf.math.log(ensemble_size)


def gibbs_cross_entropy(labels, logits):
  """Average cross entropy for ensemble members (Gibbs cross entropy).

  For each datapoint (x,y), the ensemble's Gibbs cross entropy is:

  ```
  GCE = - (1/ensemble_size) sum_{m=1}^ensemble_size log p(y|x,theta_m).
  ```

  The Gibbs cross entropy approximates the average cross entropy of a single
  model drawn from the (Gibbs) ensemble.

  Args:
    labels: tf.Tensor of shape [...].
    logits: tf.Tensor of shape [ensemble_size, ..., num_classes].

  Returns:
    tf.Tensor of shape [...].
  """
  labels = tf.cast(labels, tf.int32)
  logits = tf.convert_to_tensor(logits)
  nll = tf.nn.sparse_softmax_cross_entropy_with_logits(
      tf.broadcast_to(labels[tf.newaxis, ...], tf.shape(logits)[:-1]),
      logits)
  return tf.reduce_mean(nll, axis=0)


def main(argv):
  del argv  # unused arg
  if FLAGS.num_cores > 1:
    raise ValueError('Only a single accelerator is currently supported.')
  tf.enable_v2_behavior()
  tf.random.set_seed(FLAGS.seed)

  # TODO(trandustin): Replace with load_distributed_dataset. Currently hangs.
  dataset_train = utils.load_dataset(tfds.Split.TRAIN, FLAGS.dataset)
  dataset_test = utils.load_dataset(tfds.Split.TEST, FLAGS.dataset)
  dataset_train = dataset_train.batch(FLAGS.per_core_batch_size)
  dataset_test = dataset_test.batch(FLAGS.per_core_batch_size)
  ds_info = tfds.builder(FLAGS.dataset).info

  model = deterministic.wide_resnet(
      input_shape=ds_info.features['image'].shape,
      depth=28,
      width_multiplier=10,
      num_classes=ds_info.features['label'].num_classes,
      l2=0.,
      version=2)
  logging.info('Model input shape: %s', model.input_shape)
  logging.info('Model output shape: %s', model.output_shape)
  logging.info('Model number of weights: %s', model.count_params())

  # Search for checkpoints from their index file; then remove the index suffix.
  ensemble_filenames = tf.io.gfile.glob(os.path.join(FLAGS.output_dir,
                                                     '**/*.index'))
  ensemble_filenames = [filename[:-6] for filename in ensemble_filenames]
  ensemble_size = len(ensemble_filenames)
  logging.info('Ensemble size: %s', ensemble_size)
  logging.info('Ensemble number of weights: %s',
               ensemble_size * model.count_params())
  logging.info('Ensemble filenames: %s', str(ensemble_filenames))
  checkpoint = tf.train.Checkpoint(model=model)

  # Collect the logits output for each ensemble member and train/test data
  # point. We also collect the labels.
  # TODO(trandustin): Refactor data loader so you can get the full dataset in
  # memory without looping.
  logits_train = []
  logits_test = []
  labels_train = []
  labels_test = []
  start_time = time.time()
  for m, ensemble_filename in enumerate(ensemble_filenames):
    checkpoint.restore(ensemble_filename)
    logits = []
    logging.info('Working on training data for ensemble member %s', m)
    for features, labels in dataset_train:
      logits.append(model(features, training=False))
      if m == 0:
        labels_train.append(labels)

    logits = tf.concat(logits, axis=0)
    logits_train.append(logits)
    if m == 0:
      labels_train = tf.concat(labels_train, axis=0)

    logging.info('Working on test data for ensemble member %s', m)
    logits = []
    for features, labels in dataset_test:
      logits.append(model(features, training=False))
      if m == 0:
        labels_test.append(labels)

    logits = tf.concat(logits, axis=0)
    logits_test.append(logits)
    if m == 0:
      labels_test = tf.concat(labels_test, axis=0)

    batch_size = FLAGS.per_core_batch_size
    steps_per_epoch = ds_info.splits['train'].num_examples // batch_size
    steps_per_eval = ds_info.splits['test'].num_examples // batch_size
    current_step = (steps_per_epoch + steps_per_eval) * (m + 1)
    max_steps = (steps_per_epoch + steps_per_eval) * ensemble_size
    time_elapsed = time.time() - start_time
    steps_per_sec = float(current_step) / time_elapsed
    eta_seconds = (max_steps - current_step) / steps_per_sec
    message = ('{:.1%} completion: ensemble member {:d}/{:d}. {:.1f} steps/s. '
               'ETA: {:.0f} min. Time elapsed: {:.0f} min'.format(
                   (m + 1) / ensemble_size,
                   m + 1,
                   ensemble_size,
                   steps_per_sec,
                   eta_seconds / 60,
                   time_elapsed / 60))

  metrics = {}

  # Compute the ensemble's NLL and Gibbs cross entropy for each data point.
  # Then average over the dataset.
  nll_train = ensemble_negative_log_likelihood(labels_train, logits_train)
  nll_test = ensemble_negative_log_likelihood(labels_test, logits_test)
  gibbs_ce_train = gibbs_cross_entropy(labels_train, logits_train)
  gibbs_ce_test = gibbs_cross_entropy(labels_test, logits_test)
  metrics['train_negative_log_likelihood'] = tf.reduce_mean(nll_train)
  metrics['test_negative_log_likelihood'] = tf.reduce_mean(nll_test)
  metrics['train_gibbs_cross_entropy'] = tf.reduce_mean(gibbs_ce_train)
  metrics['test_gibbs_cross_entropy'] = tf.reduce_mean(gibbs_ce_test)

  # Given the per-element logits tensor of shape [ensemble_size, dataset_size,
  # num_classes], average over the ensemble members' probabilities. Then
  # compute accuracy and average over the dataset.
  probs_train = tf.reduce_mean(tf.nn.softmax(logits_train), axis=0)
  probs_test = tf.reduce_mean(tf.nn.softmax(logits_test), axis=0)
  accuracy_train = tf.keras.metrics.sparse_categorical_accuracy(labels_train,
                                                                probs_train)
  accuracy_test = tf.keras.metrics.sparse_categorical_accuracy(labels_test,
                                                               probs_test)
  metrics['train_accuracy'] = tf.reduce_mean(accuracy_train)
  metrics['test_accuracy'] = tf.reduce_mean(accuracy_test)
  logging.info('Metrics: %s', metrics)

if __name__ == '__main__':
  app.run(main)
