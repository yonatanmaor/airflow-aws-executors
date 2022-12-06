"""
**Default AWS Batch configuration**
This is the default configuration for calling the Batch `submit_job` function.
The AWS Batch Executor calls Boto3's submit_job(**kwargs) function with the kwargs templated by this
dictionary. See the URL below for documentation on the parameters accepted by the Boto3 submit_job function.
In other words, if you don't like the way Airflow calls the Boto3 SubmitJob API, then send your own kwargs
by overriding the airflow config file.
.. seealso::
https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/batch.html#Batch.Client.submit_job
:return: Dictionary kwargs to be used by Batch submit_job() function.


**Default AWS ECS configuration**
This is the default configuration for calling the ECS `run_task` function.
The AWS ECS-Fargate Executor calls Boto3's run_task(**kwargs) function with the kwargs templated by this
dictionary. See the URL below for documentation on the parameters accepted by the Boto3 run_task function.
In other words, if you don't like the way Airflow calls the Boto3 RunTask API, then send your own kwargs
by overriding the airflow config file.

.. seealso::
https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs.html#ECS.Client.run_task
:return: Dictionary kwargs to be used by ECS run_task() function.
"""

from airflow.configuration import conf


def has_option(section, config_name) -> bool:
    """Returns True if configuration has a section and an option"""
    if conf.has_option(section, config_name):
        config_val = conf.get(section, config_name)
        return config_val is not None and config_val != ''
    return False


BATCH_SUBMIT_JOB_KWARGS = {}
if conf.has_option('batch', 'region'):
    BATCH_SUBMIT_JOB_KWARGS = {
        'jobName': conf.get('batch', 'job_name'),
        'jobQueue': conf.get('batch', 'job_queue'),
        'jobDefinition': conf.get('batch', 'job_definition'),
        'containerOverrides': {
            'command': []
        }
    }

'''
networkConfiguration={
        'awsvpcConfiguration': {
            'subnets': [
                'string',
            ],
            'securityGroups': [
                'string',
            ],
            'assignPublicIp': 'ENABLED'|'DISABLED'
        }
    },
'''


ECS_FARGATE_RUN_TASK_KWARGS = {}
if conf.has_option('ecs_fargate', 'region'):
    ECS_FARGATE_RUN_TASK_KWARGS = {
        'cluster': conf.get('ecs_fargate', 'cluster'),
        'taskDefinition': conf.get('ecs_fargate', 'task_definition'),
        'platformVersion': 'LATEST',
        'networkConfiguration': {
        'awsvpcConfiguration': {
                'subnets': [
                    'subnet-0c565ff6127f7446b',
                    'subnet-0c09dfb78b8032ccf'
                ],
                'securityGroups': [
                    'sg-09f5332efd78b38dd',
                ],
                'assignPublicIp': 'DISABLED'
            }
        },
        'overrides': {
            'containerOverrides': [{
                'name': conf.get('ecs_fargate', 'container_name'),
                # The executor will overwrite the 'command' property during execution.
                # Must always be the first container!
                'command': []
            }]
        },
        'count': 1
    }

    if has_option('ecs_fargate', 'launch_type'):
        ECS_FARGATE_RUN_TASK_KWARGS['launchType'] = conf.get('ecs_fargate', 'launch_type')

    # Only build this section if 'subnets', 'security_groups', and 'assign_public_ip' are populated
    if (has_option('ecs_fargate', 'subnets') and has_option('ecs_fargate', 'security_groups') and
            conf.has_option('ecs_fargate', 'assign_public_ip')):
        ECS_FARGATE_RUN_TASK_KWARGS['networkConfiguration'] = {
            'awsvpcConfiguration': {
                'subnets': conf.get('ecs_fargate', 'subnets').split(','),
                'securityGroups': conf.get('ecs_fargate', 'security_groups').split(','),
                'assignPublicIp': conf.get('ecs_fargate', 'assign_public_ip')
            }
        }
