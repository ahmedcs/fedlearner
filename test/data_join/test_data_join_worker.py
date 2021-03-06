# Copyright 2020 The FedLearner Authors. All Rights Reserved.
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

# coding: utf-8

import os
import threading
from os import listdir
from os.path import isfile, join
import time
import random
import logging
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import unittest
import tensorflow.compat.v1 as tf
tf.enable_eager_execution()
import numpy as np
import tensorflow_io
from tensorflow.compat.v1 import gfile
from google.protobuf import text_format, empty_pb2, timestamp_pb2

import grpc

from fedlearner.common import common_pb2 as common_pb
from fedlearner.common import data_join_service_pb2 as dj_pb
from fedlearner.common import data_join_service_pb2_grpc as dj_grpc
from fedlearner.common.mysql_client import DBClient

from fedlearner.proxy.channel import make_insecure_channel, ChannelType
from fedlearner.data_join import (
    data_block_manager, common,
    data_join_master, data_join_worker,
    raw_data_visitor, raw_data_publisher
)
from fedlearner.data_join.data_block_manager import DataBlockBuilder
from fedlearner.data_join.raw_data_iter_impl.tf_record_iter import TfExampleItem

class DataJoinWorker(unittest.TestCase):
    def setUp(self):
        db_database = 'test_mysql'
        db_addr = 'localhost:2379'
        db_username_l = 'test_user_l'
        db_username_f = 'test_user_f'
        db_password_l = 'test_password_l'
        db_password_f = 'test_password_f'
        db_base_dir_l = 'byefl_l'
        db_base_dir_f= 'byefl_f'
        data_source_name = 'test_data_source'
        kvstore_l = DBClient(db_database, db_addr, db_username_l,
                              db_password_l, db_base_dir_l, True)
        kvstore_f = DBClient(db_database, db_addr, db_username_f,
                              db_password_f, db_base_dir_f, True)
        kvstore_l.delete_prefix(common.data_source_kvstore_base_dir(data_source_name))
        kvstore_f.delete_prefix(common.data_source_kvstore_base_dir(data_source_name))
        data_source_l = common_pb.DataSource()
        self.raw_data_pub_dir_l = './raw_data_pub_dir_l'
        data_source_l.raw_data_sub_dir = self.raw_data_pub_dir_l
        data_source_l.role = common_pb.FLRole.Leader
        data_source_l.state = common_pb.DataSourceState.Init
        data_source_l.output_base_dir = "./ds_output_l"
        self.raw_data_dir_l = "./raw_data_l"
        data_source_f = common_pb.DataSource()
        self.raw_data_pub_dir_f = './raw_data_pub_dir_f'
        data_source_f.role = common_pb.FLRole.Follower
        data_source_f.raw_data_sub_dir = self.raw_data_pub_dir_f
        data_source_f.state = common_pb.DataSourceState.Init
        data_source_f.output_base_dir = "./ds_output_f"
        self.raw_data_dir_f = "./raw_data_f"
        data_source_meta = common_pb.DataSourceMeta()
        data_source_meta.name = data_source_name
        data_source_meta.partition_num = 2
        data_source_meta.start_time = 0
        data_source_meta.end_time = 100000000
        data_source_l.data_source_meta.MergeFrom(data_source_meta)
        common.commit_data_source(kvstore_l, data_source_l)
        data_source_f.data_source_meta.MergeFrom(data_source_meta)
        common.commit_data_source(kvstore_f, data_source_f)

        self.kvstore_l = kvstore_l
        self.kvstore_f = kvstore_f
        self.data_source_l = data_source_l
        self.data_source_f = data_source_f
        self.data_source_name = data_source_name
        self.db_database = db_database
        self.db_addr = db_addr
        self.db_username_l = db_username_l
        self.db_username_f = db_username_f
        self.db_password_l = db_password_l
        self.db_password_f = db_password_f
        self.db_base_dir_l = db_base_dir_l
        self.db_base_dir_f = db_base_dir_f
        self.raw_data_publisher_l = raw_data_publisher.RawDataPublisher(
                self.kvstore_l, self.raw_data_pub_dir_l
            )
        self.raw_data_publisher_f = raw_data_publisher.RawDataPublisher(
                self.kvstore_f, self.raw_data_pub_dir_f
            )
        if gfile.Exists(data_source_l.output_base_dir):
            gfile.DeleteRecursively(data_source_l.output_base_dir)
        if gfile.Exists(self.raw_data_dir_l):
            gfile.DeleteRecursively(self.raw_data_dir_l)
        if gfile.Exists(data_source_f.output_base_dir):
            gfile.DeleteRecursively(data_source_f.output_base_dir)
        if gfile.Exists(self.raw_data_dir_f):
            gfile.DeleteRecursively(self.raw_data_dir_f)

        self.worker_options = dj_pb.DataJoinWorkerOptions(
                use_mock_etcd=True,
                raw_data_options=dj_pb.RawDataOptions(
                    raw_data_iter='TF_RECORD',
                    read_ahead_size=1<<20,
                    read_batch_size=128,
                    optional_fields=['label']
                ),
                example_id_dump_options=dj_pb.ExampleIdDumpOptions(
                    example_id_dump_interval=1,
                    example_id_dump_threshold=1024
                ),
                example_joiner_options=dj_pb.ExampleJoinerOptions(
                    example_joiner='STREAM_JOINER',
                    min_matching_window=64,
                    max_matching_window=256,
                    data_block_dump_interval=30,
                    data_block_dump_threshold=1000
                ),
                batch_processor_options=dj_pb.BatchProcessorOptions(
                    batch_size=512,
                    max_flying_item=2048
                ),
                data_block_builder_options=dj_pb.WriterOptions(
                    output_writer='TF_RECORD'
                )
            )
        self.total_index = 1 << 12

    def generate_raw_data(self, start_index, kvstore, rdp, data_source, raw_data_base_dir, partition_id,
                          block_size, shuffle_win_size, feat_key_fmt, feat_val_fmt):
        dbm = data_block_manager.DataBlockManager(data_source, partition_id)
        raw_data_dir = os.path.join(raw_data_base_dir,
                                    common.partition_repr(partition_id))
        if not gfile.Exists(raw_data_dir):
            gfile.MakeDirs(raw_data_dir)
        useless_index = 0
        new_raw_data_fnames = []
        for block_index in range(start_index // block_size, (start_index + self.total_index) // block_size):
            builder = DataBlockBuilder(
                    raw_data_base_dir,
                    data_source.data_source_meta.name,
                    partition_id, block_index,
                    dj_pb.WriterOptions(output_writer='TF_RECORD'), None
                )
            cands = list(range(block_index * block_size, (block_index + 1) * block_size))
            start_index = cands[0]
            for i in range(len(cands)):
                if random.randint(1, 4) > 2:
                    continue
                a = random.randint(i - shuffle_win_size, i + shuffle_win_size)
                b = random.randint(i - shuffle_win_size, i + shuffle_win_size)
                if a < 0:
                    a = 0
                if a >= len(cands):
                    a = len(cands) - 1
                if b < 0:
                    b = 0
                if b >= len(cands):
                    b = len(cands) - 1
                if (abs(cands[a]-i-start_index) <= shuffle_win_size and
                        abs(cands[b]-i-start_index) <= shuffle_win_size):
                    cands[a], cands[b] = cands[b], cands[a]
            for example_idx in cands:
                feat = {}
                example_id = '{}'.format(example_idx).encode()
                feat['example_id'] = tf.train.Feature(
                        bytes_list=tf.train.BytesList(value=[example_id]))
                event_time = 150000000 + example_idx
                feat['event_time'] = tf.train.Feature(
                        int64_list=tf.train.Int64List(value=[event_time]))
                label = random.choice([1, 0])
                if random.random() < 0.8:
                    feat['label'] = tf.train.Feature(
                        int64_list=tf.train.Int64List(value=[label]))
                feat[feat_key_fmt.format(example_idx)] = tf.train.Feature(
                        bytes_list=tf.train.BytesList(
                            value=[feat_val_fmt.format(example_idx).encode()]))
                example = tf.train.Example(features=tf.train.Features(feature=feat))
                builder.append_item(TfExampleItem(example.SerializeToString()),
                                      useless_index, useless_index)
                useless_index += 1
            meta = builder.finish_data_block()
            fname = common.encode_data_block_fname(
                        data_source.data_source_meta.name,
                        meta
                    )
            new_raw_data_fnames.append(os.path.join(raw_data_dir, fname))
        fpaths = [os.path.join(raw_data_dir, f)
                    for f in gfile.ListDirectory(raw_data_dir)
                    if not gfile.IsDirectory(os.path.join(raw_data_dir, f))]
        for fpath in fpaths:
            if fpath.endswith(common.DataBlockMetaSuffix):
                gfile.Remove(fpath)
        rdp.publish_raw_data(partition_id, new_raw_data_fnames)

    def test_all_assembly(self):
        for i in range(3):
            logging.info('Testing round %d', i + 1)
            self._inner_test_round(i*self.total_index)

    def _inner_test_round(self, start_index):
        for i in range(self.data_source_l.data_source_meta.partition_num):
            self.generate_raw_data(
                    start_index, self.kvstore_l, self.raw_data_publisher_l,
                    self.data_source_l, self.raw_data_dir_l, i, 2048, 64,
                    'leader_key_partition_{}'.format(i) + ':{}',
                    'leader_value_partition_{}'.format(i) + ':{}'
                )
            self.generate_raw_data(
                    start_index, self.kvstore_f, self.raw_data_publisher_f,
                    self.data_source_f, self.raw_data_dir_f, i, 4096, 128,
                    'follower_key_partition_{}'.format(i) + ':{}',
                    'follower_value_partition_{}'.format(i) + ':{}'
                )

        master_addr_l = 'localhost:4061'
        master_addr_f = 'localhost:4062'
        master_options = dj_pb.DataJoinMasterOptions(use_mock_etcd=True,
                                                     batch_mode=True)
        master_l = data_join_master.DataJoinMasterService(
                int(master_addr_l.split(':')[1]), master_addr_f,
                self.data_source_name, self.db_database, self.db_base_dir_l,
                self.db_addr, self.db_username_l, self.db_password_l,
                master_options,
            )
        master_l.start()
        master_f = data_join_master.DataJoinMasterService(
                int(master_addr_f.split(':')[1]), master_addr_l,
                self.data_source_name, self.db_database, self.db_base_dir_f,
                self.db_addr, self.db_username_f, self.db_password_f,
                master_options
            )
        master_f.start()
        channel_l = make_insecure_channel(master_addr_l, ChannelType.INTERNAL)
        master_client_l = dj_grpc.DataJoinMasterServiceStub(channel_l)
        channel_f = make_insecure_channel(master_addr_f, ChannelType.INTERNAL)
        master_client_f = dj_grpc.DataJoinMasterServiceStub(channel_f)

        while True:
            try:
                req_l = dj_pb.DataSourceRequest(
                        data_source_meta=self.data_source_l.data_source_meta
                    )
                req_f = dj_pb.DataSourceRequest(
                        data_source_meta=self.data_source_f.data_source_meta
                    )
                dss_l = master_client_l.GetDataSourceStatus(req_l)
                dss_f = master_client_f.GetDataSourceStatus(req_f)
                self.assertEqual(dss_l.role, common_pb.FLRole.Leader)
                self.assertEqual(dss_f.role, common_pb.FLRole.Follower)
                if dss_l.state == common_pb.DataSourceState.Processing and \
                        dss_f.state == common_pb.DataSourceState.Processing:
                    break
            except Exception as e:
                pass
            time.sleep(2)

        worker_addr_l = 'localhost:4161'
        worker_addr_f = 'localhost:4162'

        worker_l = data_join_worker.DataJoinWorkerService(
                int(worker_addr_l.split(':')[1]),
                worker_addr_f, master_addr_l, 0,
                self.db_database, self.db_base_dir_l,
                self.db_addr, self.db_username_l,
                self.db_password_l, self.worker_options
            )

        worker_f = data_join_worker.DataJoinWorkerService(
                int(worker_addr_f.split(':')[1]),
                worker_addr_l, master_addr_f, 0,
                self.db_database, self.db_base_dir_f,
                self.db_addr, self.db_username_f,
                self.db_password_f, self.worker_options
            )

        th_l = threading.Thread(target=worker_l.run, name='worker_l')
        th_f = threading.Thread(target=worker_f.run, name='worker_f')

        th_l.start()
        th_f.start()

        while True:
            try:
                req_l = dj_pb.DataSourceRequest(
                        data_source_meta=self.data_source_l.data_source_meta
                    )
                req_f = dj_pb.DataSourceRequest(
                        data_source_meta=self.data_source_f.data_source_meta
                    )
                dss_l = master_client_l.GetDataSourceStatus(req_l)
                dss_f = master_client_f.GetDataSourceStatus(req_f)
                self.assertEqual(dss_l.role, common_pb.FLRole.Leader)
                self.assertEqual(dss_f.role, common_pb.FLRole.Follower)
                if dss_l.state == common_pb.DataSourceState.Ready and \
                        dss_f.state == common_pb.DataSourceState.Ready:
                    break
            except Exception as e: #xx
                pass
            time.sleep(2)

        th_l.join()
        th_f.join()
        master_l.stop()
        master_f.stop()

    def tearDown(self):
        if gfile.Exists(self.data_source_l.output_base_dir):
            gfile.DeleteRecursively(self.data_source_l.output_base_dir)
        if gfile.Exists(self.raw_data_dir_l):
            gfile.DeleteRecursively(self.raw_data_dir_l)
        if gfile.Exists(self.data_source_f.output_base_dir):
            gfile.DeleteRecursively(self.data_source_f.output_base_dir)
        if gfile.Exists(self.raw_data_dir_f):
            gfile.DeleteRecursively(self.raw_data_dir_f)
        self.kvstore_f.delete_prefix(common.data_source_kvstore_base_dir(self.db_base_dir_f))
        self.kvstore_l.delete_prefix(common.data_source_kvstore_base_dir(self.db_base_dir_l))

if __name__ == '__main__':
    unittest.main()
