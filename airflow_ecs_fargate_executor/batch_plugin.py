"""AWS ECS Executor. Each Airflow task gets deligated out to an AWS ECS or Fargate Task"""

import time
from copy import deepcopy

from typing import Any, Dict, List, Optional, Tuple

import boto3
from airflow.configuration import conf
from airflow.executors.base_executor import BaseExecutor
from airflow.utils.module_loading import import_string
from airflow.utils.state import State
from marshmallow import Schema, fields, post_load

CommandType = List[str]
TaskInstanceKeyType = Tuple[Any]
ExecutorConfigType = Dict[str, Any]


DEFAULT_BATCH_KWARGS = {
    'jobName': conf.get('batch', 'job_name'),
    'jobQueue': conf.get('batch', 'job_queue'),
    'jobDefinition': conf.get('batch', 'job_definition'),
    'containerOverrides': {
        'command': []
    }
}


class BatchJob:
    """
    Data Transfer Object for an ECS Fargate Task
    """
    STATE_MAPPINGS = {
        'SUBMITTED': State.QUEUED,
        'PENDING': State.QUEUED,
        'RUNNABLE': State.QUEUED,
        'STARTING': State.QUEUED,
        'RUNNING': State.RUNNING,
        'SUCCEEDED': State.SUCCESS,
        'FAILED': State.FAILED
    }

    def __init__(self, job_id: str, status: str, status_reason: Optional[str] = None):
        self.job_id = job_id
        self.status = status
        self.status_reason = status_reason

    def get_task_state(self) -> str:
        """
        This is the primary logic that handles state in an AWS Batch Task
        """
        return self.STATE_MAPPINGS.get(self.status, State.QUEUED)

    def __repr__(self):
        return '({} -> {}, {})'.format(self.job_id, self.status, self.get_task_state())


class AwsBatchExecutor(BaseExecutor):
    """
    The Airflow Scheduler create a shell command, and passes it to the executor. This ECS Executor simply
    runs said airflow command on a remote AWS Fargate or AWS ECS Cluster with an task-definition configured
    with the same containers as the Scheduler. It then periodically checks in with the launched tasks
    (via task-arns) to determine the status.
    This allows individual tasks to specify CPU, memory, GPU, env variables, etc. When initializing a task,
    there's an option for "executor config" which should be a dictionary with keys that match the "ContainerOverride"
    definition per AWS' documentation (see link below).
    Prerequisite: proper configuration of Boto3 library
    .. seealso:: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/configuration.html for
    authentication and access-key management. You can store an environmental variable, setup aws config from
    console, or use IAM roles.
    .. seealso:: https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ContainerOverride.html for an
     Airflow TaskInstance's executor_config.
    """
    # AWS only allows a maximum number of JOBs in the describe_jobs function
    DESCRIBE_JOBS_BATCH_SIZE = 99

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_workers: Optional[BatchTaskCollection] = None
        self.batch = None
        self.submit_job_kwargs = None

    def start(self):
        """Initialize Boto3 ECS Client, and other internal variables"""
        region = conf.get('batch', 'region')
        self.active_workers = BatchTaskCollection()
        self.batch = boto3.client('batch', region_name=region)
        self.submit_job_kwargs = self.__load_submit_kwargs()

    def sync(self):
        """Checks and update state on all running tasks"""
        all_task_arns = self.active_workers.get_all_jobs()
        if not all_task_arns:
            self.log.debug("No active tasks, skipping sync")
            return

        describe_job_response = self._describe_tasks(all_task_arns)
        self.log.debug('Active Workers: %s', describe_job_response)

        for job in describe_job_response:
            if job.get_task_state() == State.FAILED:
                task_key = self.active_workers.pop_by_id(job.job_id)
                self.fail(task_key)
            elif job.get_task_state() == State.SUCCESS:
                task_key = self.active_workers.pop_by_id(job.job_id)
                self.success(task_key)

    def _describe_tasks(self, job_ids) -> List[BatchJob]:
        all_jobs = []
        max_batch_size = self.__class__.DESCRIBE_JOBS_BATCH_SIZE
        for i in range((len(job_ids) // max_batch_size) + 1):
            batched_job_ids = job_ids[i * max_batch_size: (i + 1) * max_batch_size]
            boto_describe_tasks = self.batch.describe_jobs(jobs=batched_job_ids)
            describe_tasks_response = BatchDescribeJobsResponseSchema().load(boto_describe_tasks)
            if describe_tasks_response.errors:
                self.log.error('Batch DescribeJobs API Response: %s', boto_describe_tasks)
                raise BatchError(
                    'DescribeJobs API call does not match expected JSON shape. '
                    'Are you sure that the correct version of Boto3 is installed? {}'.format(
                        describe_tasks_response.errors
                    )
                )
            all_jobs.extend(describe_tasks_response.data['jobs'])
        return all_jobs

    def execute_async(self, key: TaskInstanceKeyType, command: CommandType, queue=None, executor_config=None):
        """
        Save the task to be executed in the next sync using Boto3's RunTask API
        """
        if executor_config and ('name' in executor_config or 'command' in executor_config):
            raise ValueError('Executor Config should never override "name" or "command"')
        job_id = self._run_task(command, executor_config)
        self.active_workers.add_job(job_id, key)

    def _run_task(self, cmd: CommandType, exec_config: ExecutorConfigType) -> Dict[str, Any]:
        """
        The command and executor config will be placed in the container-override section of the JSON request, before
        calling Boto3's "run_task" function.
        """
        submit_job_api = deepcopy(self.submit_job_kwargs)
        submit_job_api['containerOverrides'].update(exec_config)
        submit_job_api['containerOverrides']['command'] = cmd
        boto_run_task = self.batch.submit_job(**submit_job_api)
        run_task_response = BatchSubmitJobResponseSchema().load(boto_run_task)
        if run_task_response.errors:
            self.log.error('ECS RunTask Response: %s', run_task_response)
            raise BatchError(
                'RunTask API call does not match expected JSON shape. '
                'Are you sure that the correct version of Boto3 is installed? {}'.format(
                    run_task_response.errors
                )
            )
        return run_task_response.data['job_id']

    def end(self, heartbeat_interval=10):
        """
        Waits for all currently running tasks to end, and doesn't launch any tasks
        """
        while True:
            self.sync()
            if not self.active_workers:
                break
            time.sleep(heartbeat_interval)

    def terminate(self):
        """
        Kill all ECS processes by calling Boto3's StopTask API.
        """
        for job_id in self.active_workers.get_all_jobs():
            self.batch.terminate_job(
                job_id=job_id,
                reason='Airflow Executor received a SIGTERM'
            )
        self.end()

    @staticmethod
    def __load_submit_kwargs() -> dict:
        submit_kwargs = import_string(
            conf.get('batch', 'submit_job_kwargs')
        )
        # Sanity check with some helpful errors
        assert isinstance(submit_kwargs, dict)

        if 'containerOverrides' not in submit_kwargs or 'command' not in submit_kwargs['containerOverrides']:
            raise KeyError('SubmitJob API needs kwargs["containerOverrides"]["command"] field,'
                           ' and value should be NULL.')
        return submit_kwargs


class BatchTaskCollection:
    """
    A Two-way dictionary between Airflow task ids and Batch Job IDs
    """
    def __init__(self):
        self.key_to_id: Dict[TaskInstanceKeyType, str] = {}
        self.id_to_key: Dict[str, TaskInstanceKeyType] = {}

    def add_job(self, job_id: str, airflow_task_key: TaskInstanceKeyType):
        """Adds a task to the collection"""
        self.key_to_id[airflow_task_key] = job_id
        self.id_to_key[job_id] = airflow_task_key

    def pop_by_id(self, job_id: str) -> TaskInstanceKeyType:
        """Deletes task from collection based off of Batch Job ID"""
        task_key = self.id_to_key[job_id]
        del self.key_to_id[task_key]
        del self.id_to_key[job_id]
        return task_key

    def get_all_jobs(self) -> List[str]:
        """Get all AWS ARNs in collection"""
        return list(self.id_to_key.keys())

    def __len__(self):
        """Determines the number of tasks in collection"""
        return len(self.key_to_id)


class BatchSubmitJobResponseSchema(Schema):
    # The unique identifier for the job.
    job_id = fields.String(load_from='jobId', required=True)


class BatchJobDetailSchema(Schema):
    # The unique identifier for the job.
    job_id = fields.String(load_from='jobId', required=True)
    # The current status for the job: 'SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING', 'RUNNING', 'SUCCEEDED', 'FAILED'
    status = fields.String(required=True)
    # A short, human-readable string to provide additional details about the current status of the job.
    status_reason = fields.String(load_from='statusReason')

    @post_load
    def make_task(self, data, **kwargs):
        """Overwrites marshmallow data property to return an instance of BotoTask instead of a dictionary"""
        return BatchJob(**data)


class BatchDescribeJobsResponseSchema(Schema):
    # The list of jobs
    jobs = fields.List(fields.Nested(BatchJobDetailSchema), required=True)


class BatchError(Exception):
    """Thrown when something unexpected has occurred within the AWS ECS/Fargate ecosystem"""

