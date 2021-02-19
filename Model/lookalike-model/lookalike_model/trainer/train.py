import os
import time
import pickle
import random
import numpy as np
import tensorflow as tf
import sys
from input import DataInput, DataInputTest
from model import Model
from sklearn.metrics import roc_auc_score

# os.environ['CUDA_VISIBLE_DEVICES'] = "0,1,2,3"
random.seed(1234)
np.random.seed(1234)
tf.set_random_seed(1234)

train_batch_size = 1024
test_batch_size = 512
predict_batch_size = 32
predict_users_num = 1000
predict_ads_num = 30
time_interval_num = 30

with open('ad_dataset_gdin_30.pkl', 'rb') as f:

  train_set = pickle.load(f)
  test_set = pickle.load(f)
  cate_list = pickle.load(f)
  user_count, item_count, cate_count = pickle.load(f)

with open('label_gdin_30.pkl', 'rb') as f:
    test_lb = pickle.load(f)
test_set_with_label = []
for i in range(len(test_lb)):
    test_set_with_label.append(tuple([test_set[i][0], test_set[i][1], test_set[i][2][0], test_lb[i]]))

best_auc = 0.0
best_auc_train = 0.0
best_auc_test = 0.0
def calc_auc(raw_arr):
    """Summary

    Args:
        raw_arr (TYPE): Description

    Returns:
        TYPE: Description
    """
    # sort by pred value, from small to big
    arr = sorted(raw_arr, key=lambda d:d[2])

    auc = 0.0
    fp1, tp1, fp2, tp2 = 0.0, 0.0, 0.0, 0.0
    for record in arr:
        fp2 += record[0] # noclick
        tp2 += record[1] # click
        auc += (fp2 - fp1) * (tp2 + tp1)
        fp1, tp1 = fp2, tp2

    # if all nonclick or click, disgard
    threshold = len(arr) - 1e-3
    if tp2 > threshold or fp2 > threshold:
        return -0.5

    if tp2 * fp2 > 0.0:  # normal auc
        return (1.0 - auc / (2.0 * tp2 * fp2))
    else:
        return None

def _auc_arr(score):
  score_p = score[:,0]
  score_n = score[:,1]
  #print "============== p ============="
  #print score_p
  #print "============== n ============="
  #print score_n
  score_arr = []
  for s in score_p.tolist():
    score_arr.append([0, 1, s])
  for s in score_n.tolist():
    score_arr.append([1, 0, s])
  return score_arr
def _eval(sess, model):
  auc_sum = 0.0
  score_arr = []
  for _, uij in DataInputTest(test_set, test_batch_size):
    auc_, score_ = model.eval(sess, uij)
    score_arr += _auc_arr(score_)
    auc_sum += auc_ * len(uij[0])
  test_gauc = auc_sum / len(test_set)
  Auc = calc_auc(score_arr)
  global best_auc
  if best_auc < test_gauc:
    best_auc = test_gauc
    model.save(sess, 'save_path/ckpt')
  return test_gauc, Auc


def _eval_logdata(sess, model, dataset, batch_size, type):
    score_arr = []
    y = []
    for _, uij in DataInput(dataset, batch_size):
        score_ = model.eval_logdata(sess, uij)
        score_arr.append(np.squeeze(score_[0]))
        y.append(np.asarray(uij[2]))
    # score_arr = np.vstack(score_arr)
    score_arr = np.hstack(score_arr)
    y = np.hstack(np.asarray(y))
    Auc = roc_auc_score(y, score_arr)

    global best_auc_train
    global best_auc_test

    if type=='train' and best_auc_train<Auc:
        best_auc_train = Auc
    if type=='test' and best_auc_test<Auc:
        best_auc_test = Auc
        model.save(sess, 'save_path/ckpt')
    return Auc


def _test(sess, model):
  auc_sum = 0.0
  score_arr = []
  predicted_users_num = 0
  print("test sub items")
  for _, uij in DataInputTest(test_set, predict_batch_size):
    if predicted_users_num >= predict_users_num:
        break
    score_ = model.test(sess, uij)
    score_arr.append(score_)
    predicted_users_num += predict_batch_size
  return score_[0]

if min(cate_list)>0:
    item_count += 1

gpu_options = tf.GPUOptions(allow_growth=True)
with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options)) as sess:

  model = Model(user_count, item_count, cate_count, cate_list, predict_batch_size, predict_ads_num)
  sess.run(tf.global_variables_initializer())
  sess.run(tf.local_variables_initializer())

  print('test_gauc: %.4f\t test_auc: %.4f' % _eval(sess, model))
  sys.stdout.flush()
  lr = 0.1
  start_time = time.time()
  for _ in range(250):

    random.shuffle(train_set)

    epoch_size = round(len(train_set) / train_batch_size)
    loss_sum = 0.0
    cnt = 0
    epoch_total_loss = 0.0
    for _, uij in DataInput(train_set, train_batch_size):
      loss = model.train(sess, uij, lr)
      loss_sum += loss
      epoch_total_loss += loss

      #if model.global_step.eval() % 1000 == 0:
      #  test_gauc, Auc = _eval(sess, model)
      #  print('Epoch %d Global_step %d\tTrain_loss: %.4f\tEval_GAUC: %.4f\tEval_AUC: %.4f' %
      #        (model.global_epoch_step.eval(), model.global_step.eval(),
      #         loss_sum / 1000, test_gauc, Auc))
      #  sys.stdout.flush()
      #  loss_sum = 0.0
      cnt += 1
      
      if model.global_step.eval() % 336000 == 0:
        lr = 1.0

    epoch_mean_loss = epoch_total_loss / cnt
    auc_train = _eval_logdata(sess, model, train_set, train_batch_size, 'train')
    auc_test = _eval_logdata(sess, model, test_set_with_label, test_batch_size, 'test')
    print('Epoch %d DONE\tGlobal step: %d\tCost time: %.2f\ttrain epoch average loss: %.6f\ttrain auc: %.4f\ttest auc: %.4f' %
          (model.global_epoch_step.eval(), model.global_step.eval(), time.time()-start_time, epoch_mean_loss, auc_train, auc_test))
    sys.stdout.flush()
    model.global_epoch_step_op.eval()

  print('best train_gauc %.4f\ttest_gauc: %.4f' % (best_auc_train, best_auc_test))
  sys.stdout.flush()
