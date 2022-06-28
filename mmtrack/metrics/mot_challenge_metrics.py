# Copyright (c) OpenMMLab. All rights reserved.
import os
import os.path as osp
import shutil
import tempfile
from collections import defaultdict
from typing import List, Optional, Sequence, Union

import numpy as np
import trackeval
from mmengine.dist import broadcast_object_list, is_main_process

from mmtrack.metrics import BaseVideoMetric
from mmtrack.registry import METRICS


@METRICS.register_module()
class MOTChallengeMetrics(BaseVideoMetric):
    """Evaluation metrics for MOT Challenge.

    Args:
        metric (str | list[str]): Metrics to be evaluated. Options are
            'HOTA', 'CLEAR', 'Identity'.
            Defaults to ['HOTA', 'CLEAR', 'Identity'].
        resfile_path (str, optional): Path to save the formatted results.
            Defaults to None.
        track_iou_thr (float): IoU threshold for tracking evaluation.
            Defaults to 0.5.
        benchmark (str): Benchmark to be evaluated. Defaults to 'MOT17'.
        format_only (bool): If True, only formatting the results to the
            official format and not performing evaluation. Defaults to False.
        collect_device (str): Device name used for collecting results from
            different ranks during distributed training. Must be 'cpu' or
            'gpu'. Defaults to 'cpu'.
        prefix (str, optional): The prefix that will be added in the metric
            names to disambiguate homonymous metrics of different evaluators.
            If prefix is not provided in the argument, self.default_prefix
            will be used instead. Default: None
    Returns:
    """
    TRACKER = 'default-tracker'
    allowed_metrics = ['HOTA', 'CLEAR', 'Identity']
    allowed_benchmarks = ['MOT15', 'MOT16', 'MOT17', 'MOT20']
    default_prefix: Optional[str] = 'motchallenge-metric'

    def __init__(self,
                 metric: Union[str, List[str]] = ['HOTA', 'CLEAR', 'Identity'],
                 resfile_path: Optional[str] = None,
                 track_iou_thr: float = 0.5,
                 benchmark: str = 'MOT17',
                 format_only: bool = False,
                 collect_device: str = 'cpu',
                 prefix: Optional[str] = None) -> None:
        super().__init__(collect_device=collect_device, prefix=prefix)
        if isinstance(metric, list):
            metrics = metric
        elif isinstance(metric, str):
            metrics = [metric]
        else:
            raise TypeError('metric must be a list or a str.')
        for metric in metrics:
            if metric not in self.allowed_metrics:
                raise KeyError(f'metric {metric} is not supported.')
        self.metrics = metrics
        self.format_only = format_only
        assert benchmark in self.allowed_benchmarks
        self.benchmark = benchmark
        self.track_iou_thr = track_iou_thr
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.seq_info = defaultdict(
            lambda: dict(seq_length=-1, gt_strings=[], pred_strings=[]))
        self.gt_dir = self._get_output_dir('gt')
        self.pred_dir = self._get_output_dir('pred')
        self.resfile_path = self._get_resfile_path(resfile_path)

    def _get_resfile_path(self, resfile_path):
        """Get path to save the formatted results."""
        if resfile_path is None:
            resfile_path = self.tmp_dir.name
        else:
            # TODO: add an log info for "remove previous results".
            if osp.exists(resfile_path):
                shutil.rmtree(resfile_path)
        resfile_path = osp.join(resfile_path, self.TRACKER)
        return resfile_path

    def _get_output_dir(self, name: str):
        """Get directory to save the gt and prediction files."""
        output_dir = osp.join(self.tmp_dir.name, name)
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def process(self, data_batch: Sequence[dict],
                predictions: Sequence[dict]) -> None:
        """Process one batch of data samples and predictions.

        The processed results should be stored in ``self.results``, which will
        be used to compute the metrics when all batches have been processed.

        Args:
            data_batch (Sequence[dict]): A batch of data from the dataloader.
            predictions (Sequence[dict]): A batch of outputs from the model.
        """
        for data, pred in zip(data_batch, predictions):
            # load basic info
            assert 'data_sample' in data
            data_sample = data['data_sample']
            frame_id = data_sample['frame_id']
            video_length = data_sample['video_length']
            video = data_sample['img_path'].split(os.sep)[-3]
            if self.seq_info[video]['seq_length'] == -1:
                self.seq_info[video]['seq_length'] = video_length

            # load gts
            if 'instances' in data_sample:
                gt_instances = data_sample['instances']
                gt_strings = [
                    '%d,%d,%d,%d,%d,%d,%d,%d,%d\n' %
                    (frame_id + 1, gt_instances[i]['instance_id'],
                     gt_instances[i]['bbox'][0], gt_instances[i]['bbox'][1],
                     gt_instances[i]['bbox'][2], gt_instances[i]['bbox'][3],
                     gt_instances[i]['mot_conf'],
                     gt_instances[i]['mot_class_id'],
                     gt_instances[i]['visibility'])
                    for i in range(len(gt_instances))
                ]
                self.seq_info[video]['gt_strings'].extend(gt_strings)

            # load predictions
            assert 'pred_track_instances' in pred
            pred_instances = pred['pred_track_instances']
            pred_strings = [
                '%d,%d,%.3f,%.3f,%.3f,%.3f,%.3f,-1,-1,-1\n' % (
                    frame_id + 1,
                    pred_instances['instances_id'][i],
                    pred_instances['bboxes'][i][0],
                    pred_instances['bboxes'][i][1],
                    pred_instances['bboxes'][i][2],
                    pred_instances['bboxes'][i][3],
                    pred_instances['scores'][i],
                ) for i in range(len(pred_instances['instances_id']))
            ]
            self.seq_info[video]['pred_strings'].extend(pred_strings)

            if frame_id == video_length - 1:
                self._save_one_video_gts_preds(video)
                break

    def _save_one_video_gts_preds(self, seq: str) -> None:
        """Save the gt and prediction results."""
        info = self.seq_info[seq]
        # save predictions
        pred_file = osp.join(self.pred_dir, seq + '.txt')
        with open(pred_file, 'wt') as f:
            for line in info['pred_strings']:
                f.writelines(line)
        info['pred_strings'].clear()
        # save gts
        if info['gt_strings']:
            gt_file = osp.join(self.gt_dir, seq + '.txt')
            with open(gt_file, 'wt') as f:
                for line in info['gt_strings']:
                    f.writelines(line)
            info['gt_strings'].clear()

    def compute_metrics(self, results: list = None) -> dict:
        """Compute the metrics from processed results.

        Args:
            results (list): The processed results of each batch.
                Defaults to None.

        Returns:
            dict: The computed metrics. The keys are the names of the metrics,
            and the values are corresponding results.
        """
        # NOTICE: don't access `self.results` from the method.
        eval_results = dict()
        shutil.move(self.pred_dir, self.resfile_path)

        if self.format_only:
            return eval_results

        seqmap = osp.join(self.resfile_path, 'videoseq.txt')
        with open(seqmap, 'w') as f:
            f.write('name\n')
            for video in self.seq_info.keys():
                f.write(video + '\n')
            f.close()

        eval_config = trackeval.Evaluator.get_default_eval_config()

        # need to split out the tracker name
        # caused by the implementation of TrackEval
        resfile_path_tmp = self.resfile_path.rsplit(osp.sep, 1)[0]
        dataset_config = self.get_dataset_cfg(self.gt_dir, resfile_path_tmp,
                                              seqmap)

        evaluator = trackeval.Evaluator(eval_config)
        dataset = [trackeval.datasets.MotChallenge2DBox(dataset_config)]
        metrics = [
            getattr(trackeval.metrics,
                    metric)(dict(METRICS=[metric], THRESHOLD=0.5))
            for metric in self.metrics
        ]
        output_res, _ = evaluator.evaluate(dataset, metrics)
        output_res = output_res['MotChallenge2DBox'][
            self.TRACKER]['COMBINED_SEQ']['pedestrian']

        if 'HOTA' in self.metrics:
            eval_results['HOTA'] = np.average(output_res['HOTA']['HOTA'])
            eval_results['AssA'] = np.average(output_res['HOTA']['AssA'])
            eval_results['DetA'] = np.average(output_res['HOTA']['DetA'])

        if 'CLEAR' in self.metrics:
            eval_results['MOTA'] = np.average(output_res['CLEAR']['MOTA'])
            eval_results['MOTP'] = np.average(output_res['CLEAR']['MOTP'])
            eval_results['IDSW'] = np.average(output_res['CLEAR']['IDSW'])
            eval_results['TP'] = np.average(output_res['CLEAR']['CLR_TP'])
            eval_results['FP'] = np.average(output_res['CLEAR']['CLR_FP'])
            eval_results['FN'] = np.average(output_res['CLEAR']['CLR_FN'])
            eval_results['Frag'] = np.average(output_res['CLEAR']['Frag'])
            eval_results['MT'] = np.average(output_res['CLEAR']['MT'])
            eval_results['ML'] = np.average(output_res['CLEAR']['ML'])

        if 'Identity' in self.metrics:
            eval_results['IDF1'] = np.average(output_res['Identity']['IDF1'])
            eval_results['IDTP'] = np.average(output_res['Identity']['IDTP'])
            eval_results['IDFN'] = np.average(output_res['Identity']['IDFN'])
            eval_results['IDFP'] = np.average(output_res['Identity']['IDFP'])
            eval_results['IDP'] = np.average(output_res['Identity']['IDP'])
            eval_results['IDR'] = np.average(output_res['Identity']['IDR'])

        self.tmp_dir.cleanup()
        return eval_results

    def evaluate(self, size: int = None) -> dict:
        """Evaluate the model performance of the whole dataset after processing
        all batches.

        Args:
            size (int): Length of the entire validation dataset.
                Defaults to None.

        Returns:
            dict: Evaluation metrics dict on the val dataset. The keys are the
            names of the metrics, and the values are corresponding results.
        """
        if is_main_process():
            _metrics = self.compute_metrics()  # type: ignore
            # Add prefix to metric names
            if self.prefix:
                _metrics = {
                    '/'.join((self.prefix, k)): v
                    for k, v in _metrics.items()
                }
            metrics = [_metrics]
        else:
            metrics = [None]  # type: ignore

        broadcast_object_list(metrics)

        # reset the results list
        self.results.clear()
        return metrics[0]

    def get_dataset_cfg(self, gt_folder: str, tracker_folder: str,
                        seqmap: str):
        """Get default configs for trackeval.datasets.MotChallenge2DBox.

        Args:
            gt_folder (str): the name of the GT folder
            tracker_folder (str): the name of the tracker folder
            seqmap (str): the file that contains the sequence of video names

        Returns:
            Dataset Configs for MotChallenge2DBox.
        """
        dataset_config = dict(
            # Location of GT data
            GT_FOLDER=gt_folder,
            # Trackers location
            TRACKERS_FOLDER=tracker_folder,
            # Where to save eval results
            # (if None, same as TRACKERS_FOLDER)
            OUTPUT_FOLDER=None,
            # Use self.TRACKER as the default tracker
            TRACKERS_TO_EVAL=[self.TRACKER],
            # Option values: ['pedestrian']
            CLASSES_TO_EVAL=list(self._dataset_meta['CLASSES']),
            # Option Values: 'MOT15', 'MOT16', 'MOT17', 'MOT20'
            BENCHMARK=self.benchmark,
            # Option Values: 'train', 'test'
            SPLIT_TO_EVAL='train',
            # Whether tracker input files are zipped
            INPUT_AS_ZIP=False,
            # Whether to print current config
            PRINT_CONFIG=True,
            # Whether to perform preprocessing
            # (never done for MOT15)
            DO_PREPROC=False if self.benchmark == 'MOT15' else True,
            # Tracker files are in
            # TRACKER_FOLDER/tracker_name/TRACKER_SUB_FOLDER
            TRACKER_SUB_FOLDER='',
            # Output files are saved in
            # OUTPUT_FOLDER/tracker_name/OUTPUT_SUB_FOLDER
            OUTPUT_SUB_FOLDER='',
            # Names of trackers to display
            # (if None: TRACKERS_TO_EVAL)
            TRACKER_DISPLAY_NAMES=None,
            # Where seqmaps are found
            # (if None: GT_FOLDER/seqmaps)
            SEQMAP_FOLDER=None,
            # Directly specify seqmap file
            # (if none use seqmap_folder/benchmark-split_to_eval)
            SEQMAP_FILE=seqmap,
            # If not None, specify sequences to eval
            # and their number of timesteps
            # SEQ_INFO=None,
            SEQ_INFO={
                seq: info['seq_length']
                for seq, info in self.seq_info.items()
            },
            # '{gt_folder}/{seq}.txt'
            GT_LOC_FORMAT='{gt_folder}/{seq}.txt',
            # If False, data is in GT_FOLDER/BENCHMARK-SPLIT_TO_EVAL/ and in
            # TRACKERS_FOLDER/BENCHMARK-SPLIT_TO_EVAL/tracker/
            # If True, the middle 'benchmark-split' folder is skipped for both.
            SKIP_SPLIT_FOL=True,
        )

        return dataset_config