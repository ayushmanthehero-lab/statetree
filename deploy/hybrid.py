"""Publish and explicitly deploy StateTree's bounded hybrid AWS demo.

Cloud mutations require --apply. `plan` is offline; no hosting starts during publish.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build' / 'hybrid'
OCI = 'application/vnd.oci.image.manifest.v1+json'
DOCKER = 'application/vnd.docker.distribution.manifest.v2+json'


def validate_name(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,38}[a-z0-9]', value):
        raise ValueError('Stack name must be 2-40 lowercase letters/digits/hyphens.')
    return value


def convert_manifest(source):
    """Convert only supported single-image gzip metadata, preserving layer content."""
    result = copy.deepcopy(source)
    if result.get('mediaType') == DOCKER:
        return result
    if result.get('mediaType') != OCI or result.get('schemaVersion') != 2:
        raise ValueError('Expected a single OCI/Docker image, not an image index.')
    if result.get('config', {}).get('mediaType') != 'application/vnd.oci.image.config.v1+json':
        raise ValueError('Unsupported image config media type.')
    for layer in result.get('layers', []):
        if layer.get('mediaType') != 'application/vnd.oci.image.layer.v1.tar+gzip':
            raise ValueError('Expected gzip layers. Rebuild with Docker gzip compression.')
        layer['mediaType'] = 'application/vnd.docker.image.rootfs.diff.tar.gzip'
    result['mediaType'] = DOCKER
    result['config']['mediaType'] = 'application/vnd.docker.container.image.v1+json'
    return result


def assert_owned(tags, name):
    if not any(t.get('Key') == 'StateTreeHybrid' and t.get('Value') == name for t in tags):
        raise ValueError('Resource name is already used by an unmanaged resource; refusing to change it.')


def select_public_subnets(subnets, tables):
    main = next((t for t in tables if any(a.get('Main') for a in t.get('Associations', []))), {})
    chosen = {}
    for subnet in sorted(subnets, key=lambda s: s['SubnetId']):
        table = next((t for t in tables if any(a.get('SubnetId') == subnet['SubnetId']
                                              for a in t.get('Associations', []))), main)
        public_route = any(r.get('DestinationCidrBlock') == '0.0.0.0/0'
                           and r.get('GatewayId', '').startswith('igw-')
                           and r.get('State') == 'active' for r in table.get('Routes', []))
        if subnet.get('MapPublicIpOnLaunch') and public_route:
            chosen.setdefault(subnet['AvailabilityZone'], subnet['SubnetId'])
    if len(chosen) < 2:
        raise ValueError('Need default-VPC public subnets with an internet-gateway route in two zones. '
                         'No VPC or NAT gateway was created. Restore a suitable default VPC first.')
    return sorted(chosen.values())


def session_for(args):
    import boto3
    return boto3.Session(profile_name=args.aws_profile, region_name=args.region)


def pages(client, operation, key, **kwargs):
    for page in client.get_paginator(operation).paginate(**kwargs):
        yield from page.get(key, [])


def error_code(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code', '')


def preflight(session):
    sq = session.client('service-quotas')
    quotas = list(pages(sq, 'list_service_quotas', 'Quotas', ServiceCode='fargate'))
    def quota(match):
        found = [q['Value'] for q in quotas if match(q['QuotaName'].lower())]
        if len(found) != 1:
            raise ValueError(f'Cannot identify required {service} quota. Inspect Service Quotas manually.')
        return found[0]
    vcpu_limit = quota(lambda n: 'on-demand vcpu resource count' in n)
    ecs = session.client('ecs')
    used_cpu = 0
    for cluster in pages(ecs, 'list_clusters', 'clusterArns'):
        for desired in ('RUNNING', 'PENDING'):
            arns = list(pages(ecs, 'list_tasks', 'taskArns', cluster=cluster, desiredStatus=desired))
            for start in range(0, len(arns), 100):
                detail = ecs.describe_tasks(cluster=cluster, tasks=arns[start:start+100])
                if detail.get('failures'):
                    raise ValueError('Could not inspect existing ECS tasks; quota headroom is unknown. Retry preflight.')
                for task in detail['tasks']:
                    if task.get('launchType') == 'FARGATE' and task.get('capacityProviderName') != 'FARGATE_SPOT':
                        used_cpu += int(task.get('cpu', '0')) / 1024
    if vcpu_limit - used_cpu < 2:
        raise ValueError('Insufficient spare quota: allow 2 Fargate vCPUs for a rolling web deployment. No hosting was created.')
    ec2 = session.client('ec2')
    vpcs = ec2.describe_vpcs(Filters=[{'Name': 'is-default', 'Values': ['true']}])['Vpcs']
    if len(vpcs) != 1:
        raise ValueError('A default VPC is required for this guide; none was created automatically.')
    filters = [{'Name': 'vpc-id', 'Values': [vpcs[0]['VpcId']]}]
    subnets = list(pages(ec2, 'describe_subnets', 'Subnets', Filters=filters))
    tables = list(pages(ec2, 'describe_route_tables', 'RouteTables', Filters=filters))
    selected = select_public_subnets(subnets, tables)
    session.client('cloudformation').describe_type(Type='RESOURCE', TypeName='AWS::ECS::ExpressGatewayService')
    return {'subnets': selected, 'fargate_vcpu_limit': vcpu_limit, 'fargate_vcpu_used': used_cpu}


def docker_path():
    found = shutil.which('docker')
    if found:
        return found
    for root in (Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'DockerDesktop',
                 Path('C:/Program Files/Docker/Docker')):
        candidate = root / 'resources/bin/docker.exe'
        if candidate.exists():
            return str(candidate)
    raise ValueError('Docker CLI not found. Start Docker Desktop and reopen PowerShell.')


def docker_run(command, env=None):
    executable = docker_path()
    child_env = dict(os.environ if env is None else env)
    child_env['PATH'] = str(Path(executable).parent) + os.pathsep + child_env.get('PATH', '')
    subprocess.run([executable, *command], cwd=ROOT, env=child_env, check=True)


def push_image(remote, registry, auth):
    """Use an ephemeral Docker config, without invoking Windows credential helpers."""
    host = os.environ.get('DOCKER_HOST')
    if not host or os.environ.get('DOCKER_CONTEXT'):
        inspected = subprocess.run([docker_path(), 'context', 'inspect', '--format',
                                    '{{json .Endpoints.docker}}'], capture_output=True, text=True, check=True)
        host = json.loads(inspected.stdout)['Host']
    if not host.startswith(('npipe://', 'unix://')):
        raise ValueError('This publisher expects a local Docker Desktop/Unix daemon. Remote TLS contexts need separate setup.')
    private = BUILD / 'private'
    private.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='docker-auth-', dir=private) as directory:
        config = Path(directory) / 'config.json'
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump({'auths': {registry: {'auth': auth}}}, handle)
        env = dict(os.environ)
        for name in ('DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_AUTH_CONFIG'):
            env.pop(name, None)
        docker_run(['--config', directory, '--host', host, 'push', remote], env)


def artifact_path(args):
    return BUILD / f'{args.stack}-{args.region}.json'


def publish(args):
    subprocess.run([sys.executable, '-m', 'deploy.local_agent', 'prepare'], cwd=ROOT, check=True)
    session = session_for(args)
    account = session.client('sts').get_caller_identity()['Account']
    tag = time.strftime('%Y%m%d%H%M%S', time.gmtime())
    images = {}
    for kind, context, dockerfile in (('web', '.', 'deploy/hybrid_web/Dockerfile'),):
        local = f'{args.stack}-{kind}:{tag}'
        docker_run(['buildx', 'build', '--platform', 'linux/amd64', '--provenance=false',
                    '--load', '-f', dockerfile, '-t', local, context])
        images[kind] = local
    ecr = session.client('ecr')
    tags = [{'Key': 'StateTreeHybrid', 'Value': args.stack}]
    registry = f'{account}.dkr.ecr.{args.region}.amazonaws.com'
    auth = ecr.get_authorization_token()['authorizationData'][0]['authorizationToken']
    digests = {}
    for kind, local in images.items():
        name = f'{args.stack}-{kind}'
        try:
            repo = ecr.describe_repositories(repositoryNames=[name])['repositories'][0]
            assert_owned(ecr.list_tags_for_resource(resourceArn=repo['repositoryArn'])['tags'], args.stack)
        except Exception as error:
            if error_code(error) != 'RepositoryNotFoundException':
                raise
            ecr.create_repository(repositoryName=name, tags=tags,
                                  imageScanningConfiguration={'scanOnPush': True})
        remote = f'{registry}/{name}:{tag}'
        docker_run(['tag', local, remote])
        push_image(remote, registry, auth)
        response = ecr.batch_get_image(repositoryName=name, imageIds=[{'imageTag': tag}],
                                       acceptedMediaTypes=[OCI, DOCKER])
        if not response.get('images'):
            raise ValueError('ECR did not return the pushed single-image manifest.')
        image = response['images'][0]
        manifest = json.loads(image['imageManifest'])
        if manifest.get('mediaType') != DOCKER:
            converted = json.dumps(convert_manifest(manifest), separators=(',', ':'))
            image = ecr.put_image(repositoryName=name, imageTag=tag+'-docker-v2',
                                  imageManifest=converted, imageManifestMediaType=DOCKER)['image']
        digests[kind] = f"{registry}/{name}@{image['imageId']['imageDigest']}"
    sm = session.client('secretsmanager')
    secret_name = f'{args.stack}/demo-access'
    raw_secret = (BUILD / 'private/secret.json').read_text(encoding='utf-8')
    data = json.loads(raw_secret)
    if not all(isinstance(data.get(k), str) and len(data[k]) >= 24
               for k in ('demo_access_code', 'owner_access_code', 'worker_api_key')):
        raise ValueError('Local secret file is invalid; rerun local_agent prepare.')
    try:
        detail = sm.describe_secret(SecretId=secret_name)
        assert_owned(detail.get('Tags', []), args.stack)
        # Avoid versions/restart requirements if the same keys were already published.
        if sm.get_secret_value(SecretId=secret_name)['SecretString'] != raw_secret:
            sm.put_secret_value(SecretId=secret_name, SecretString=raw_secret)
        arn = detail['ARN']
    except Exception as error:
        if error_code(error) != 'ResourceNotFoundException':
            raise
        arn = sm.create_secret(Name=secret_name, SecretString=raw_secret, Tags=tags)['ARN']
    manifest = {'account': account, 'region': args.region, 'stack': args.stack,
                'web_image': digests['web'], 'secret_arn': arn}
    BUILD.mkdir(parents=True, exist_ok=True)
    artifact_path(args).write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'published': str(artifact_path(args)), 'hosting_started': False}))


def load_artifacts(args):
    path = artifact_path(args)
    if not path.exists():
        raise ValueError('Run publish --apply first; image digest and secret ARN manifest is missing.')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data['region'] != args.region or data['stack'] != args.stack:
        raise ValueError('Published artifact region/stack mismatch.')
    for field in ('web_image',):
        prefix = f"{data['account']}.dkr.ecr.{args.region}.amazonaws.com/{args.stack}-"
        if not data[field].startswith(prefix) or not re.search(r'@sha256:[a-f0-9]{64}$', data[field]):
            raise ValueError('Published images must use ECR digest URIs for this stack and region.')
    return data


def plan(args):
    from deploy.hybrid_template import build_template
    artifacts = load_artifacts(args)
    template = build_template()
    BUILD.mkdir(parents=True, exist_ok=True)
    path = BUILD / f'{args.stack}-template.json'
    path.write_text(json.dumps(template, indent=2)+'\n', encoding='utf-8')
    params = [{'ParameterKey': key, 'ParameterValue': value} for key, value in {
        'WebImage': artifacts['web_image'], 'SecretArn': artifacts['secret_arn'],
        'BedrockModelId': args.bedrock_model_id}.items()]
    (BUILD / f'{args.stack}-parameters.json').write_text(json.dumps(params, indent=2)+'\n', encoding='utf-8')
    return template, params, artifacts


def get_stack(client, name):
    try:
        return client.describe_stacks(StackName=name)['Stacks'][0]
    except Exception as error:
        if error_code(error) == 'ValidationError' and 'does not exist' in str(error):
            return None
        raise


def deploy(args):
    template, params, artifacts = plan(args)
    session = session_for(args)
    if session.client('sts').get_caller_identity()['Account'] != artifacts['account']:
        raise ValueError('AWS account differs from the account used to publish the images.')
    cf = session.client('cloudformation')
    existing = get_stack(cf, args.stack)
    if existing:
        assert_owned(existing.get('Tags', []), args.stack)
        if not args.update:
            raise ValueError('Stack already exists. Use status, or deploy --update --apply for a new tunnel/image.')
        if existing['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
            raise ValueError('Stack is busy or failed. Inspect status; resolve/delete it before redeployment.')
    checks = preflight(session)
    params.append({'ParameterKey': 'SubnetIds', 'ParameterValue': ','.join(checks['subnets'])})
    body = json.dumps(template)
    cf.validate_template(TemplateBody=body)
    request = dict(StackName=args.stack, TemplateBody=body, Parameters=params,
                   Capabilities=['CAPABILITY_IAM'], Tags=[{'Key': 'StateTreeHybrid', 'Value': args.stack}])
    print('Starting paid AWS hosting: Fargate + load balancer/public IP; also Bedrock inference, '
          'ECR, logs, Secrets Manager and S3. Delete the stack when finished.', flush=True)
    try:
        result = cf.update_stack(**request) if existing else cf.create_stack(**request)
    except Exception as error:
        if error_code(error) == 'ValidationError' and 'No updates are to be performed' in str(error):
            print('Stack already has these settings.')
            return
        raise
    print(json.dumps({'stack_id': result['StackId'], 'state': 'UPDATE_IN_PROGRESS' if existing else 'CREATE_IN_PROGRESS',
                      'next': 'Run python -m deploy.hybrid status; initial creation can take several minutes.'}))


def status(args):
    cf = session_for(args).client('cloudformation')
    stack = get_stack(cf, args.stack)
    if stack is None:
        print(json.dumps({'stack': args.stack, 'status': 'ABSENT'}))
        return
    assert_owned(stack.get('Tags', []), args.stack)
    result = {'stack': args.stack, 'status': stack['StackStatus'],
              'outputs': {o['OutputKey']: o['OutputValue'] for o in stack.get('Outputs', [])}}
    events = cf.describe_stack_events(StackName=args.stack)['StackEvents']
    result['recent_failures'] = [{k: e[k] for k in ('LogicalResourceId', 'ResourceStatus', 'ResourceStatusReason') if k in e}
                                 for e in events if 'FAILED' in e['ResourceStatus']][:6]
    print(json.dumps(result, indent=2))


def delete(args):
    cf = session_for(args).client('cloudformation')
    stack = get_stack(cf, args.stack)
    if stack is None:
        print('Stack is absent. ECR images, secret, retained reports and logs may still exist.')
        return
    assert_owned(stack.get('Tags', []), args.stack)
    cf.delete_stack(StackName=stack['StackId'])
    print('Deletion requested. Use status until ABSENT. Reports bucket, ECR images, secret and any retained '
          'service logs are not deleted; they can incur storage charges. Hosting bills until deletion finishes.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'publish', 'plan', 'deploy', 'status', 'delete'))
    parser.add_argument('--region', default='ap-south-1', choices=('ap-south-1',))
    parser.add_argument('--stack', default='statetree-hybrid')
    parser.add_argument('--aws-profile', default='default')
    parser.add_argument('--bedrock-model-id', default='amazon.nova-lite-v1:0')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--update', action='store_true')
    args = parser.parse_args(argv)
    try:
        validate_name(args.stack)
        if args.command in ('publish', 'deploy', 'delete') and not args.apply:
            parser.error(f'{args.command} changes AWS resources; add --apply when ready.')
        if args.command in ('plan', 'deploy') and not re.fullmatch(r'[A-Za-z0-9._:-]{3,256}', args.bedrock_model_id):
            parser.error('--bedrock-model-id must be a valid Bedrock foundation model ID.')
        if args.command == 'prepare':
            subprocess.run([sys.executable, '-m', 'deploy.local_agent', 'prepare'], cwd=ROOT, check=True)
        elif args.command == 'plan':
            plan(args)
            print(f'Offline template and parameter files saved under {BUILD}. No AWS resources changed.')
        else:
            globals()[args.command](args)
    except (Exception, KeyboardInterrupt) as error:
        # Do not print SDK request bodies, environment dictionaries or secret strings.
        if isinstance(error, KeyboardInterrupt):
            print('Interrupted. Existing cloud resources are retained; use status before retrying.', file=sys.stderr)
        elif error_code(error):
            print(f'AWS {error_code(error)}: {str(error)}', file=sys.stderr)
        else:
            print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
