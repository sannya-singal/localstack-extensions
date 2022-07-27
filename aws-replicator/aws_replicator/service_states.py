import logging
from typing import Any, Dict, Type, TypedDict

import boto3
import requests
from botocore.client import BaseClient
from localstack.config import get_edge_url
from localstack.constants import INTERNAL_RESOURCE_PATH
from localstack.services.cloudformation.models.dynamodb import DynamoDBTable
from localstack.services.cloudformation.models.s3 import S3Bucket
from localstack.services.cloudformation.models.sqs import SQSQueue
from localstack.services.cloudformation.service_models import GenericBaseModel
from localstack.utils.aws import aws_stack
from localstack.utils.threads import parallelize

from aws_replicator.config import HANDLER_PATH

LOG = logging.getLogger(__name__)


# TODO: move to patch utils
def mixin_for(wrapped_clazz: Type):
    """Decorator that adds the decorated class as a mixin to the base classes of the given class"""

    def wrapper(wrapping_clazz):
        wrapped_clazz.__bases__ = (wrapping_clazz,) + wrapped_clazz.__bases__

    return wrapper


class ReplicateStateRequest(TypedDict):
    """
    Represents a request sent from the CLI to the extension request
    handler to inject additional resource state properties.
    Using upper-case property names, to stay in line with CloudFormation/CloudControl resource models.
    """

    # resource type name (e.g., "AWS::S3::Bucket")
    Type: str
    # identifier of the resource
    PhysicalResourceId: str
    # resource properties
    Properties: Dict[str, Any]


class ExtendedResourceStateReplicator(GenericBaseModel):
    """Mixin for resource models, used to replicate (inject) additional state into a resource instance"""

    def add_extended_state_external(self, remote_client: BaseClient = None):
        """Called in the context of external CLI execution to fetch/replicate resource details from a remote account"""

    def add_extended_state_internal(self, state: Dict):
        """Called in the context of the internal LocalStack instance to inject the state into a resource"""


# resource-specific replications


@mixin_for(SQSQueue)
class StateReplicatorSQSQueue(ExtendedResourceStateReplicator):
    def add_extended_state_external(self, state: Dict = None, remote_client: BaseClient = None):
        # executing in the context of the CLI

        remote = remote_client or boto3.client("sqs")
        queue_name = self.props["QueueName"]
        queue_url = remote.get_queue_url(QueueName=queue_name)["QueueUrl"]

        messages = []
        while True:
            response = remote.receive_message(QueueUrl=queue_url, WaitTimeSeconds=1)
            msgs = response.get("Messages")
            if not msgs:
                break
            messages.extend(msgs)

        state = {**self.props, "Messages": messages}
        request = ReplicateStateRequest(
            Type=self.cloudformation_type(),
            Properties=state,
            PhysicalResourceId=queue_url,
        )
        _post_request_to_instance(request)

    def add_extended_state_internal(self, state: Dict = None):
        # executing in the context of the server
        from localstack.aws.api.sqs import Message
        from localstack.services.sqs.provider import SqsBackend

        queue_name = self.props["QueueName"]
        messages = state.get("Messages") or []
        LOG.info("Inserting %s messages into queue", len(messages), queue_name)
        for region, details in SqsBackend.regions().items():
            queue = details.queues.get(queue_name)
            if not queue:
                continue
            for message in messages:
                message.setdefault("MD5OfMessageAttributes", None)
                queue.put(Message(**message))
            break


@mixin_for(DynamoDBTable)
class StateReplicatorDynamoDBTable(ExtendedResourceStateReplicator):
    def add_extended_state_external(self, remote_client: BaseClient = None):
        table_name = self.props["TableName"]
        LOG.debug("Copying items from source table '%s' into target", table_name)

        remote = remote_client or boto3.resource("dynamodb")
        local = aws_stack.connect_to_resource("dynamodb")
        remote_table = remote.Table(table_name)
        local_table = local.Table(table_name)

        first_request = True
        response = {}
        while first_request or "LastEvaluatedKey" in response:
            kwargs = {} if first_request else {"ExclusiveStartKey": response["LastEvaluatedKey"]}
            first_request = False
            response = remote_table.scan(**kwargs)
            with local_table.batch_writer() as batch:
                for item in response["Items"]:
                    batch.put_item(Item=item)


@mixin_for(S3Bucket)
class StateReplicatorS3Bucket(ExtendedResourceStateReplicator):
    def add_extended_state_external(self, remote_client: BaseClient = None):
        bucket_name = self.props["BucketName"]
        LOG.debug("Copying items from source S3 bucket '%s' into target", bucket_name)

        remote = boto3.resource("s3")
        local = aws_stack.connect_to_resource("s3")
        remote_bucket = remote.Bucket(bucket_name)
        local_bucket = local.Bucket(bucket_name)
        # TODO: make configurable
        max_object_size = 1000 * 1000

        def copy_object(obj):
            if obj.size > max_object_size:
                LOG.debug("Skip copying large S3 object %s with %s bytes", obj.key, obj.size)
                return
            local_bucket.put_object(Key=obj.key, Body=obj.get()["Body"].read())

        parallelize(copy_object, list(remote_bucket.objects.all()), size=15)


def _post_request_to_instance(request: ReplicateStateRequest):
    url = f"{get_edge_url()}{INTERNAL_RESOURCE_PATH}{HANDLER_PATH}"
    response = requests.post(url, json=request)
    assert response.ok
    return response