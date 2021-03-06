# --------------------------------------------------------
# Faster R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick and Sean Bell
# --------------------------------------------------------

import caffe
import numpy as np
import yaml
from fast_rcnn.config import cfg
from generate_anchors import generate_anchors
from fast_rcnn.bbox_transform import bbox_transform_inv, clip_boxes
from fast_rcnn.nms_wrapper import nms

DEBUG = False

class ProposalLayer(caffe.Layer):
    """
    Outputs object detection proposals by applying estimated bounding-box
    transformations to a set of regular boxes (called "anchors").
    """

    def setup(self, bottom, top):
        # parse the layer parameter string, which must be valid YAML
        # print self.param_str
        layer_params = yaml.load(self.param_str_)

        # self._feat_stride = layer_params['feat_stride']
        self._feat_stride = [int(i) for i in layer_params['feat_stride'].split(',')]
        self._scales = layer_params.get('scales', (8, 16, 32))
        self._ratios = [0.5, 1, 2]
        self._num_anchors = len(self._scales)*len(self._ratios)
        # self._anchors = generate_anchors(scales=np.array(anchor_scales))
        # self._num_anchors = self._anchors.shape[0]

        if DEBUG:
            print 'feat_stride: {}'.format(self._feat_stride)
            print 'anchors:'
            print self._anchors

        # rois blob: holds R regions of interest, each is a 5-tuple
        # (n, x1, y1, x2, y2) specifying an image batch index n and a
        # rectangle (x1, y1, x2, y2)
        top[0].reshape(1, 5)
        top[1].reshape(1, 5)
        top[2].reshape(1, 5)
        top[3].reshape(1, 5)
        # scores blob: holds scores for R regions of interest
        # if len(top) > 1:
        #     top[1].reshape(1, 1, 1, 1)

    def forward(self, bottom, top):
        # Algorithm:
        #
        # for each (H, W) location i
        #   generate A anchor boxes centered on cell i
        #   apply predicted bbox deltas at cell i to each of the A anchors
        # clip predicted boxes to image
        # remove predicted boxes with either height or width < threshold
        # sort all (proposal, score) pairs by score from highest to lowest
        # take top pre_nms_topN proposals before NMS
        # apply NMS with threshold 0.7 to remaining proposals
        # take after_nms_topN proposals after NMS
        # return the top proposals (-> RoIs top, scores top)

        assert bottom[0].data.shape[0] == 1, \
            'Only single item batches are supported'

        cfg_key = str(self.phase) # either 'TRAIN' or 'TEST'
        pre_nms_topN  = cfg[cfg_key].RPN_PRE_NMS_TOP_N
        post_nms_topN = cfg[cfg_key].RPN_POST_NMS_TOP_N
        nms_thresh    = cfg[cfg_key].RPN_NMS_THRESH
        min_size      = cfg[cfg_key].RPN_MIN_SIZE

        # the first set of _num_anchors channels are bg probs
        # the second set are the fg probs, which we want
        # scores = bottom[0].data[:, self._num_anchors:, :, :]
        # bbox_deltas = bottom[1].data
        im_info = bottom[0].data[0, :]
        cls_prob_dict = {
            'stride32': bottom[8].data,
            'stride16': bottom[7].data,
            'stride8': bottom[6].data,
            'stride4': bottom[5].data,
        }
        bbox_pred_dict = {
            'stride32': bottom[4].data,
            'stride16': bottom[3].data,
            'stride8': bottom[2].data,
            'stride4': bottom[1].data,
        }

        if DEBUG:
            print 'im_size: ({}, {})'.format(im_info[0], im_info[1])
            print 'scale: {}'.format(im_info[2])

        # 1. Generate proposals from bbox deltas and shifted anchors
        proposal_list = []
        score_list = []
        for s in self._feat_stride:
            stride = int(s)
            sub_anchors = generate_anchors(base_size=stride, scales=np.array(self._scales), ratios=self._ratios)
    
            scores = cls_prob_dict['stride' + str(s)][:, self._num_anchors:, :, :]
            bbox_deltas = bbox_pred_dict['stride' + str(s)]
          
            # 1. Generate proposals from bbox_deltas and shifted anchors
            # use real image size instead of padded feature map sizes
            #height, width = int(im_info[0] / stride), int(im_info[1] / stride)
            height, width = scores.shape[-2:]

            if DEBUG:
                print 'score map size: {}'.format(scores.shape)

            # Enumerate all shifts
            shift_x = np.arange(0, width) * stride
            shift_y = np.arange(0, height) * stride
            shift_x, shift_y = np.meshgrid(shift_x, shift_y)
            shifts = np.vstack((shift_x.ravel(), shift_y.ravel(),
                                shift_x.ravel(), shift_y.ravel())).transpose()

            # Enumerate all shifted anchors:
            #
            # add A anchors (1, A, 4) to
            # cell K shifts (K, 1, 4) to get
            # shift anchors (K, A, 4)
            # reshape to (K*A, 4) shifted anchors
            A = self._num_anchors
            K = shifts.shape[0]
            anchors = sub_anchors.reshape((1, A, 4)) + \
                    shifts.reshape((1, K, 4)).transpose((1, 0, 2))
            anchors = anchors.reshape((K * A, 4))

            # Transpose and reshape predicted bbox transformations to get them
            # into the same order as the anchors:
            #
            # bbox deltas will be (1, 4 * A, H, W) format
            # transpose to (1, H, W, 4 * A)
            # reshape to (1 * H * W * A, 4) where rows are ordered by (h, w, a)
            # in slowest to fastest order
            bbox_deltas = bbox_deltas.transpose((0, 2, 3, 1)).reshape((-1, 4))

            # Same story for the scores:
            #
            # scores are (1, A, H, W) format
            # transpose to (1, H, W, A)
            # reshape to (1 * H * W * A, 1) where rows are ordered by (h, w, a)
            scores = scores.transpose((0, 2, 3, 1)).reshape((-1, 1))

            # Convert anchors into proposals via bbox transformations
            proposals = bbox_transform_inv(anchors, bbox_deltas)

            # 2. clip predicted boxes to image
            proposals = clip_boxes(proposals, im_info[:2])

            # 3. remove predicted boxes with either height or width < threshold
            # (NOTE: convert min_size to input image scale stored in im_info[2])
            keep = _filter_boxes(proposals, min_size * im_info[2])
            proposals = proposals[keep, :]
            scores = scores[keep]

            proposal_list.append(proposals)
            score_list.append(scores)

        # 4. sort all (proposal, score) pairs by score from highest to lowest
        # 5. take top pre_nms_topN (e.g. 6000)
        proposals = np.vstack(proposal_list)
        scores = np.vstack(score_list)
        order = scores.ravel().argsort()[::-1]
        if pre_nms_topN > 0:
            order = order[:pre_nms_topN]
        proposals = proposals[order, :]
        scores = scores[order]

        # 6. apply nms (e.g. threshold = 0.7)
        # 7. take after_nms_topN (e.g. 300)
        # 8. return the top proposals (-> RoIs top)
        keep = nms(np.hstack((proposals, scores)), nms_thresh)
        if post_nms_topN > 0:
            keep = keep[:post_nms_topN]
        proposals = proposals[keep, :]
        scores = scores[keep]
        #print "keep len is ", len(keep)

        # Output rois blob
        # Our RPN implementation only supports a single input image, so all
        # batch inds are 0
        w = (proposals[:,2]-proposals[:,0])
        h = (proposals[:,3]-proposals[:,1])
        s = w * h
        s[s<=0]=1e-6
        # layer_index = np.floor(k0+np.log2(np.sqrt(s)/224))
        image_area = im_info[0]*im_info[1]
        alpha = np.sqrt(h * w) / (224.0 / np.sqrt(image_area))
        layer_index_ = np.log(alpha)/np.log(2.0)

        layer_index=[]
        for i in layer_index_:
            layer_index.append(np.min([5,np.max([2,4+np.round(i).astype(np.int32)])]))

        layer_index[layer_index<2]=2
        layer_index[layer_index>5]=5
        layer_indexs = np.array(layer_index)

        rois_layers=[]

        for i in xrange(4):
            index = (layer_indexs == (i + 2))
            if np.any(index) == False:
                rois_layers.append(np.array([]))
            else:
                rois_layers.append(proposals[index,:])


        for i in xrange(4):
            if len(rois_layers[i]) == 0:
                index = i
                if index-1 >=0 and rois_layers[index-1].shape[0] > 1:
                    len_rois_layers = rois_layers[index-1].shape[0]
                    rois_layers[i]=rois_layers[index-1][len_rois_layers-1,:].reshape(1,4)
                    rois_layers[index-1]=rois_layers[index-1][0:len_rois_layers-1,:]
                elif index+1 < 4 and rois_layers[index+1].shape[0] > 1:
                    rois_layers[i]=rois_layers[index+1][0,:].reshape(1,4)
                    rois_layers[index+1]=rois_layers[index+1][1:,:]
                elif index-2 >=0 and rois_layers[index-2].shape[0] > 1:
                    len_rois_layers = rois_layers[index-2].shape[0]
                    # print len_rois_layers,'eeeeeeeeeeeee',index
                    rois_layers[i]=rois_layers[index-1][0,:].reshape(1,4)
                    rois_layers[index-1]=rois_layers[index-2][len_rois_layers-1,:].reshape(1,4)
                    # rois_layers[i]=rois_layers[index-2][0,:].reshape(1,5)
                    rois_layers[index-2]=rois_layers[index-2][0:len_rois_layers-1,:]
                elif index+2 < 4 and rois_layers[index+2].shape[0] > 1 :
                    # print rois_layers[index+1]
                    # print rois_layers[index+1][0,:]
                    # print rois_layers[index+2][0,:]
                    # print rois_layers[index+2]
                    if rois_layers[index+1].shape[0] == 0:
                        rois_layers[i+1]=rois_layers[index+2][1,:].reshape(1,4)
                        rois_layers[i]=rois_layers[index+2][0,:].reshape(1,4)
                        rois_layers[index+2]=rois_layers[index+2][2:,:]
                    else:
                        rois_layers[i]=rois_layers[index+1][0,:].reshape(1,4)
                        rois_layers[i+1]=rois_layers[index+2][0,:].reshape(1,4)
                        rois_layers[index+2]=rois_layers[index+2][1:,:]
                elif index-3 >=0 and rois_layers[index-3].shape[0] > 1:
                    len_rois_layers = rois_layers[index-3].shape[0]
                    # print len_rois_layers,'ddddddddddddd',index
                    rois_layers[i]=rois_layers[index-1][0,:].reshape(1,4)
                    rois_layers[index-1]=rois_layers[index-2][0,:].reshape(1,4)
                    rois_layers[index-2]=rois_layers[index-3][len_rois_layers-1,:]
                    # rois_layers[i]=rois_layers[index-2][0,:].reshape(1,5)
                    rois_layers[index-3]=rois_layers[index-3][0:len_rois_layers-1,:]
                elif index+3 < 4 and rois_layers[index+3].shape[0] > 1 :
                    len_rois_layers = rois_layers[index+3].shape[0]
                    rois_layers[i]=rois_layers[index+1][0,:].reshape(1,4)
                    rois_layers[index+1]=rois_layers[index+2][0,:].reshape(1,4)
                    rois_layers[index+2]=rois_layers[index+3][0,:].reshape(1,4)
                    # rois_layers[i]=rois_layers[index-2][0,:].reshape(1,5)
                    rois_layers[index+3]=rois_layers[index+3][1:,:]

        # [Optional] output scores blob
        # if len(top) > 1:
        #     top[1].reshape(*(scores.shape))
        #     top[1].data[...] = scores
        rpn_rois = np.zeros((proposals.shape[0], proposals.shape[1]), dtype=np.float32)
        count = 0
        for i in xrange(4):
            batch_inds_i = np.zeros((rois_layers[i].shape[0], 1), dtype=np.float32)
            blob_i = np.hstack((batch_inds_i, rois_layers[i].astype(np.float32, copy=False)))
            top[i].reshape(*(blob_i.shape))
            top[i].data[...] = blob_i
            rpn_rois[count:rois_layers[i].shape[0]+count,:]=rois_layers[i]
            count += rois_layers[i].shape[0]

        batch_inds= np.zeros((rpn_rois.shape[0], 1), dtype=np.float32)
        blob_rpn_rois = np.hstack((batch_inds, rpn_rois.astype(np.float32, copy=False)))
        top[4].reshape(*(blob_rpn_rois.shape))
        top[4].data[...] = blob_rpn_rois

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass

def _filter_boxes(boxes, min_size):
    """Remove all boxes with any side smaller than min_size."""
    ws = boxes[:, 2] - boxes[:, 0] + 1
    hs = boxes[:, 3] - boxes[:, 1] + 1
    keep = np.where((ws >= min_size) & (hs >= min_size))[0]
    return keep
