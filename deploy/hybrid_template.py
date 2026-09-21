"""CloudFormation template for the direct Amazon Bedrock task-chat deployment."""

from typing import Any


def build_template() -> dict[str, Any]:
    def ref(name): return {'Ref': name}
    def attr(name, attribute): return {'Fn::GetAtt': [name, attribute]}
    def sub(expression): return {'Fn::Sub': expression}
    def allow(actions, resources): return {'Effect': 'Allow', 'Action': actions, 'Resource': resources}

    def role(principal, *, statements=None, managed_policy=None):
        properties = {'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [{
            'Effect': 'Allow', 'Principal': {'Service': principal}, 'Action': 'sts:AssumeRole'}]}}
        if statements is not None:
            properties['Policies'] = [{'PolicyName': 'StateTreeRuntime', 'PolicyDocument': {
                'Version': '2012-10-17', 'Statement': statements}}]
        if managed_policy is not None:
            properties['ManagedPolicyArns'] = [sub('arn:${AWS::Partition}:iam::aws:policy/service-role/' + managed_policy)]
        return {'Type': 'AWS::IAM::Role', 'Properties': properties}

    image_pattern = r'[0-9]{12}\.dkr\.ecr\.ap-south-1\.amazonaws\.com/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}'
    tags = [{'Key': 'StateTreeManaged', 'Value': 'true'}]
    resources = {
        'ReportsBucket': {'Type': 'AWS::S3::Bucket', 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
            'Properties': {'PublicAccessBlockConfiguration': {'BlockPublicAcls': True, 'IgnorePublicAcls': True,
                'BlockPublicPolicy': True, 'RestrictPublicBuckets': True},
                'OwnershipControls': {'Rules': [{'ObjectOwnership': 'BucketOwnerEnforced'}]},
                'BucketEncryption': {'ServerSideEncryptionConfiguration': [{'ServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]},
                'Tags': tags}},
        'ReportsBucketPolicy': {'Type': 'AWS::S3::BucketPolicy', 'DeletionPolicy': 'Retain', 'UpdateReplacePolicy': 'Retain',
            'Properties': {'Bucket': ref('ReportsBucket'), 'PolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                'Sid': 'DenyInsecureTransport', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
                'Resource': [attr('ReportsBucket', 'Arn'), sub('${ReportsBucket.Arn}/*')],
                'Condition': {'Bool': {'aws:SecureTransport': 'false'}}}]}}},
        'WebExecutionRole': role('ecs-tasks.amazonaws.com', managed_policy='AmazonECSTaskExecutionRolePolicy'),
        'WebInfrastructureRole': role('ecs.amazonaws.com', managed_policy='AmazonECSInfrastructureRoleforExpressGatewayServices'),
        'WebTaskRole': role('ecs-tasks.amazonaws.com', statements=[
            allow('bedrock:InvokeModel', sub('arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/${BedrockModelId}')),
            allow('secretsmanager:GetSecretValue', ref('SecretArn')),
            allow('s3:ListBucket', attr('ReportsBucket', 'Arn')),
            allow(['s3:GetObject', 's3:PutObject'], sub('${ReportsBucket.Arn}/reports/*')),
            allow(['s3:GetObject', 's3:PutObject'], sub('${ReportsBucket.Arn}/private-chat/*'))]),
        'WebCluster': {'Type': 'AWS::ECS::Cluster', 'Properties': {'ClusterName': sub('${AWS::StackName}-web'), 'Tags': tags}},
        'WebLogGroup': {'Type': 'AWS::Logs::LogGroup', 'Properties': {'LogGroupName': sub('/ecs/${AWS::StackName}/web'), 'RetentionInDays': 7}},
        'WebService': {'Type': 'AWS::ECS::ExpressGatewayService', 'DependsOn': 'ReportsBucketPolicy', 'Properties': {
            'Cluster': ref('WebCluster'), 'ServiceName': sub('${AWS::StackName}-web'), 'ExecutionRoleArn': attr('WebExecutionRole', 'Arn'),
            'InfrastructureRoleArn': attr('WebInfrastructureRole', 'Arn'), 'TaskRoleArn': attr('WebTaskRole', 'Arn'),
            'Cpu': '1024', 'Memory': '2048', 'HealthCheckPath': '/ping', 'NetworkConfiguration': {'Subnets': ref('SubnetIds')},
            'ScalingTarget': {'MinTaskCount': 1, 'MaxTaskCount': 1}, 'PrimaryContainer': {
                'Image': ref('WebImage'), 'ContainerPort': 8080,
                'AwsLogsConfiguration': {'LogGroup': ref('WebLogGroup'), 'LogStreamPrefix': 'ecs'}, 'Environment': [
                    {'Name': 'BEDROCK_MODEL_ID', 'Value': ref('BedrockModelId')}, {'Name': 'AWS_REGION', 'Value': ref('AWS::Region')},
                    {'Name': 'DEMO_SECRET_ARN', 'Value': ref('SecretArn')}, {'Name': 'REPORT_BUCKET', 'Value': ref('ReportsBucket')},
                    {'Name': 'PORT', 'Value': '8080'}, {'Name': 'CHAT_MAX_OUTPUT_TOKENS', 'Value': '256'}]}, 'Tags': tags}},
    }
    return {'AWSTemplateFormatVersion': '2010-09-09', 'Description': 'StateTree task chat on ECS using Amazon Bedrock Converse.',
            'Parameters': {'WebImage': {'Type': 'String', 'AllowedPattern': image_pattern},
                'SecretArn': {'Type': 'String', 'AllowedPattern': r'arn:aws:secretsmanager:ap-south-1:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+'},
                'BedrockModelId': {'Type': 'String', 'AllowedPattern': r'[A-Za-z0-9._:-]{3,256}'},
                'SubnetIds': {'Type': 'CommaDelimitedList', 'AllowedPattern': r'subnet-[0-9a-f]+'}}, 'Resources': resources,
            'Outputs': {'WebUrl': {'Description': 'Public HTTPS demo endpoint.', 'Value': attr('WebService', 'Endpoint')},
                'ReportsBucket': {'Description': 'Private reports retained after stack deletion.', 'Value': ref('ReportsBucket')},
                'ClusterName': {'Value': ref('WebCluster')}, 'ServiceArn': {'Value': attr('WebService', 'ServiceArn')}}}