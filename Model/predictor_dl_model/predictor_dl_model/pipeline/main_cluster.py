# Copyright 2019, Futurewei Technologies
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
#                                                 * "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.

import math
import pickle
import statistics
import yaml
import argparse
import logging
import sys

from pyspark import SparkContext, SparkConf, Row
from pyspark.sql.functions import concat_ws, count, lit, col, udf, expr, collect_list, explode, avg, stddev, rand
from pyspark.sql.types import IntegerType, StringType, MapType, ArrayType, FloatType, BooleanType
from pyspark.sql import HiveContext
from datetime import datetime, timedelta


import transform as transform


def _save_as_table(df, table_name, hive_context, create_table):

    if create_table:
        command = """
            DROP TABLE IF EXISTS {}
            """.format(table_name)

        hive_context.sql(command)

        df.createOrReplaceTempView("r900_temp_table")

        command = """
            CREATE TABLE IF NOT EXISTS {} as select * from r900_temp_table
            """.format(table_name)

        hive_context.sql(command)


def estimate_number_of_non_dense_clusters(df, median_popularity_of_dense):
    # find avg of non-dense popularity
    median_non_dense_p = df.filter('sparse=True').agg(
        expr('percentile_approx(p, 0.5)').alias('_nondensp')).take(1)[0]['_nondensp']

    no_of_items_in_a_cluster = median_popularity_of_dense / median_non_dense_p

    no_of_cluster = df.filter('sparse=True').count() * \
        1.0 / no_of_items_in_a_cluster / 3.0

    return int(no_of_cluster) + 1


def list_to_map(mlist):
    count_map = {}
    for item in mlist:
        if item not in count_map:
            count_map[item] = 0
        count_map[item] += 1
    sum_of_values = sum(count_map.values())
    for k, v in count_map.items():
        count_map[k] = v*1.0/sum_of_values
    return count_map


def agg_ts(mlist):
    # mlsit size is prepare_past_days
    l = len(mlist[0])
    result = [0 for _ in range(l)]
    for ts in mlist:
        for i in range(len(ts)):
            n = ts[i]
            if not n:
                n = 0
            result[i] += n
    return result


def agg_on_uckey_price_cat(df):

    column_names = ['ts', 'a', 'g', 't', 'si', 'r']
    agg_exprs = [collect_list(col).alias(col) for col in column_names]
    df = df.groupBy('uckey', 'price_cat').agg(*agg_exprs)

    list_to_map_udf = udf(list_to_map, MapType(
        StringType(), FloatType(), False))
    for column_name in column_names:
        if column_name == 'ts':
            continue
        column_name_agg = column_name + '_agg'
        df = df.withColumn(column_name_agg, list_to_map_udf(column_name))
        df = df.drop(column_name)
        df = df.withColumnRenamed(column_name_agg, column_name)

    ts_agg_udf = udf(agg_ts, ArrayType(IntegerType()))
    df = df.withColumn('ts_agg', ts_agg_udf(df.ts))
    df = df.drop('ts')
    df = df.withColumnRenamed('ts_agg', 'ts')

    return df


def is_spare(datapoints_threshold, popularity_norm):
    def _helper(p_n, ts):
        num_list = [_ for _ in ts if _ is not None and _ != 0]
        if (len(num_list) * 1.0 > datapoints_threshold * len(ts) and p_n >= popularity_norm):
            return False
        return True
    return _helper


def is_non_spiked_uckey(whole_popularity_avg, popularity_th, datapoints_min_th):
    def _helper(p, ts):
        num_list = [_ for _ in ts if _ is not None and _ != 0]
        return not(p > whole_popularity_avg and len(num_list) * 1.0 < datapoints_min_th * len(ts))
    return _helper


def remove_weak_uckeys(df, popularity_th, datapoints_min_th):
    df = df.filter(udf(lambda p: p >= popularity_th, BooleanType())(df.p))
    whole_popularity_avg = df.agg(avg('p').alias('_avg')).take(1)[0]['_avg']
    df = df.filter(udf(is_non_spiked_uckey(whole_popularity_avg,
                                           popularity_th, datapoints_min_th), BooleanType())(df.p, df.ts))
    return df


def run(hive_context, cluster_size_cfg, input_table_name, pre_cluster_table_name, output_table_name, create_pre_cluster_table):

    datapoints_th_uckeys = cluster_size_cfg['datapoints_th_uckeys']
    datapoints_th_clusters = cluster_size_cfg['datapoints_th_clusters']
    popularity_norm = cluster_size_cfg['popularity_norm']
    median_popularity_of_dense = cluster_size_cfg['median_popularity_of_dense']
    number_of_virtual_clusters = cluster_size_cfg['number_of_virtual_clusters']
    popularity_th = cluster_size_cfg['popularity_th']
    datapoints_min_th = cluster_size_cfg['datapoints_min_th']

    # Read factdata table
    command = """
    select ts,price_cat,uckey,a,g,t,si,r from {}
    """.format(input_table_name)

    # DataFrame[uckey: string, price_cat: string, ts: array<int>, a: string, g: string, t: string, si: string, r: string]
    df = hive_context.sql(command)

    # add imp
    df = df.withColumn('imp', udf(lambda ts: sum(
        [_ for _ in ts if _]), IntegerType())(df.ts))

    # add popularity = mean
    df = df.withColumn('p', udf(lambda ts: sum(
        [_ for _ in ts if _])/(1.0*len(ts)), FloatType())(df.ts))

    # remove weak uckeys
    df = remove_weak_uckeys(df, popularity_th, datapoints_min_th)

    # add normalized popularity = mean_n
    df, _ = transform.normalize_ohe_feature(df, ohe_feature='p')

    df = df.withColumn('sparse', udf(
        is_spare(datapoints_th_uckeys, popularity_norm), BooleanType())(df.p_n, df.ts))

    if number_of_virtual_clusters <= 0:
        number_of_virtual_clusters = estimate_number_of_non_dense_clusters(
            df, median_popularity_of_dense)

    df = df.withColumn("cn", (rand()*1000000 %
                              number_of_virtual_clusters).cast('int'))

    if create_pre_cluster_table:
        _save_as_table(df, pre_cluster_table_name, hive_context, True)

    # change the uckey of sparse to cn
    df = df.withColumn('new_uckey', udf(lambda uckey, cn, sparse: str(
        cn) if sparse else uckey, StringType())(df.uckey, df.cn, df.sparse))
    df = df.drop('uckey')
    df = df.withColumnRenamed('new_uckey', 'uckey')

    df = agg_on_uckey_price_cat(df)

    # replace nan and zero with median
    df = transform.replace_nan_with_zero(df)

    # add imp
    df = df.withColumn('imp', udf(lambda ts: sum(
        [_ for _ in ts if _]), IntegerType())(df.ts))

    # add popularity = mean
    df = df.withColumn('p', udf(lambda ts: sum(
        [_ for _ in ts if _])/(1.0*len(ts)), FloatType())(df.ts))

    # add normalized popularity = mean_n
    df, _ = transform.normalize_ohe_feature(df, ohe_feature='p')

    df = df.filter(udf(lambda p_n, ts: not is_spare(datapoints_th_clusters, -
                                                    sys.maxsize-1)(p_n, ts), BooleanType())(df.p_n, df.ts))

    _save_as_table(df, output_table_name, hive_context, True)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='Prepare data')
    parser.add_argument('config_file')
    args = parser.parse_args()

    # Load config file
    with open(args.config_file, 'r') as ymlfile:
        cfg = yaml.load(ymlfile)

    cfg_log = cfg['log']
    cfg = cfg['pipeline']

    sc = SparkContext()
    hive_context = HiveContext(sc)
    sc.setLogLevel(cfg_log['level'])

    output_table_name = cfg['uckey_clustring']['output_table_name']
    pre_cluster_table_name = cfg['uckey_clustring']['pre_cluster_table_name']
    create_pre_cluster_table = cfg['uckey_clustring']['create_pre_cluster_table']
    input_table_name = cfg['time_series']['output_table_name']
    cluster_size_cfg = cfg['uckey_clustring']['cluster_size']

    run(hive_context=hive_context,
        cluster_size_cfg=cluster_size_cfg,
        input_table_name=input_table_name,
        pre_cluster_table_name=pre_cluster_table_name,
        output_table_name=output_table_name,
        create_pre_cluster_table=create_pre_cluster_table)

    sc.stop()
