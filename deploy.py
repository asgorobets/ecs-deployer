from __future__ import print_function

import os
import boto3
import traceback
import click
import subprocess
import logging
import click_log
import sys
from botocore.exceptions import WaiterError
from pprint import pformat

logger = logging.getLogger(__name__)
click_log.basic_config(logger)

DEFAULT_COLOR = "blue"
OTHER_COLOR = "green"
COLORS = [DEFAULT_COLOR, OTHER_COLOR]

elbclient = boto3.client('elbv2')
ecsclient = boto3.client('ecs')
ec2client = boto3.client('ec2')


def get_elb_arn(elbname):
    try:
        logger.debug(f"ELB ARN lookup by name: {elbname}")
        elbresponse = elbclient.describe_load_balancers(Names=[elbname])
        logger.debug("ELB response: " + pformat(elbresponse))
        return elbresponse['LoadBalancers'][0]['LoadBalancerArn']

    except Exception as e:
        logger.error(
            f"""
            Requested ELB could not be found: {elbname}
            Make sure the ELB is created using Terraform template
            Exception: {e.strerror}
            """
        )
        sys.exit(1)


def get_target_groups(elbname):
    """
    Returns target groups and listener rules info for live and beta deployment
    :param elbname: 
    :return: 
    """
    listeners = elbclient.describe_listeners(LoadBalancerArn=get_elb_arn(elbname))
    for x in listeners['Listeners']:
        if (x['Port'] == 443):
            livelistenerarn = x['ListenerArn']
        if (x['Port'] == 80):
            livelistenerarn = x['ListenerArn']
        if (x['Port'] == 8443):
            betalistenerarn = x['ListenerArn']
        if (x['Port'] == 8080):
            betalistenerarn = x['ListenerArn']

    livetgresponse = elbclient.describe_rules(ListenerArn=livelistenerarn)

    for x in livetgresponse['Rules']:
        if x['Conditions'] and x['Conditions'][0]['Field'] == 'path-pattern' and x['Conditions'][0]['Values'] == ['*']:
            livetargetgroup = x['Actions'][0]['TargetGroupArn']
            liverulearn = x['RuleArn']

    betatgresponse = elbclient.describe_rules(ListenerArn=betalistenerarn)

    for x in betatgresponse['Rules']:
        if x['Conditions'] and x['Conditions'][0]['Field'] == 'path-pattern' and x['Conditions'][0]['Values'] == ['*']:
            betatargetgroup = x['Actions'][0]['TargetGroupArn']
            betarulearn = x['RuleArn']

    return {
        'live': {
            'target-group-arn': livetargetgroup,
            'elb-listener-rule-arn': liverulearn,
        },
        'beta': {
            'target-group-arn': betatargetgroup,
            'elb-listener-rule-arn': betarulearn,
        }
    }


def swaptargetgroups(elbname):
    """Discovers the live target group and non-production target group and swaps

            Args:
                elbname : name of the load balancer, which has the target groups to swap

            Raises:
                Exception: Any exception thrown by handler

    """
    state = get_target_groups(elbname)
    modifyOnBeta = elbclient.modify_rule(
        RuleArn=state['beta']['elb-listener-rule-arn'],
        Actions=[
            {
                'Type': 'forward',
                'TargetGroupArn': state['live']['target-group-arn']
            }
        ]
    )

    modifyOnLive = elbclient.modify_rule(
        RuleArn=state['live']['elb-listener-rule-arn'],
        Actions=[
            {
                'Type': 'forward',
                'TargetGroupArn': state['beta']['target-group-arn']
            }
        ]
    )

    modify_tags(state['live']['target-group-arn'], "IsProduction", "False")
    modify_tags(state['beta']['target-group-arn'], "IsProduction", "True")


def modify_tags(arn, tagkey, tagvalue):
    """Modifies the tags on the target groups as an identifier, after swap has been performed to indicate, 
        which target group is live and which target group is non-production

                Args:
                    arn : AWS ARN of the Target Group
                    tagkey: Key of the Tag
                    tagvalue: Value of the Tag

                Raises:
                    Exception: Any exception thrown by handler

    """

    elbclient.add_tags(
        ResourceArns=[arn],
        Tags=[
            {
                'Key': tagkey,
                'Value': tagvalue
            },
        ]
    )


def get_service_name(project_name, env, component=None, color=None):
    return '-'.join(filter(None, [project_name, env, color, component]))


def get_target_group_arn_by_color(elbname, color):
    response = elbclient.describe_target_groups(LoadBalancerArn=get_elb_arn(elbname))

    for tg in response['TargetGroups']:
        if color in tg['TargetGroupName']:
            return tg['TargetGroupArn']

    raise RuntimeError(f"No target group found for requested elb: {elbname} and color: {color}")


def get_current_color(elbname, is_blue_green):
    if not is_blue_green:
        return DEFAULT_COLOR

    state = get_target_groups(elbname)

    for color in COLORS:
        if state['live']['target-group-arn'] == get_target_group_arn_by_color(elbname, color):
            return color

    raise RuntimeError('Live environment color identification failed')


def get_opposite_color(color):
    return OTHER_COLOR if color == DEFAULT_COLOR else DEFAULT_COLOR


def get_service(cluster_name, service_name):
    response = ecsclient.describe_services(
        cluster=cluster_name,
        services=[
            service_name,
        ]
    )

    try:
        if response['services'][0]['status'] == 'ACTIVE':
            return response['services'][0]
    except IndexError:
        pass

    return False


def get_elb_name(project_name, env):
    return f"{project_name}-{env}"


def get_target_color_from_context(ctx):
    if not ctx.obj['is-blue-green']:
        return DEFAULT_COLOR

    live_service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['component'], ctx.obj['current-color'])
    live_service = get_service(ctx.obj['cluster'], live_service_name)

    # Target the inactive color in case that live_service exists, otherwise target live color for initialization.
    return get_opposite_color(ctx.obj['current-color']) if live_service else ctx.obj['current-color']


def run_cli_command(ctx, command, additional_env=None):
    if additional_env is None:
        additional_env = {}

    if ctx.obj['populate-target-env']:
        target_env = {
            "TARGET_COLOR": ctx.obj['target-color'],
            "OPPOSITE_COLOR": get_opposite_color(ctx.obj['target-color'])
        }
    else:
        target_env = {}

    env_list = []
    if target_env or additional_env:
        for k, v in {**target_env, **additional_env}.items():
            env_list.append(f"{k}={v}")

    click.secho(f"{' '.join(env_list)} {command}".lstrip(), fg='green')
    subprocess.run(command, shell=True, env={**os.environ, **target_env, **additional_env}).check_returncode()


def run_service_command(ctx, command, additional_env=None):
    target_service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['component'], ctx.obj['target-color'])
    cmd = f"ecs-cli compose --project-name {target_service_name} --cluster {ctx.obj['cluster']} --region {ctx.obj['region']} --ecs-params {ctx.obj['ecs-params']} " + command

    run_cli_command(ctx, cmd, additional_env)


def get_status_tasks_records(ctx, service_name):
    tasks_records = []
    response = ecsclient.list_tasks(
        cluster=ctx.obj['cluster'],
        serviceName=service_name,
    )
    task_arns = response['taskArns']
    if not task_arns:
        return []

    tasks_list = ecsclient.describe_tasks(
        cluster=ctx.obj['cluster'],
        tasks=task_arns
    )
    for task in tasks_list['tasks']:

        container_instances = ecsclient.describe_container_instances(
            cluster=ctx.obj['cluster'],
            containerInstances=[
                task['containerInstanceArn'],
            ]
        )

        container_instance = container_instances['containerInstances'][0]

        instances = ec2client.describe_instances(
            InstanceIds=[
                container_instance['ec2InstanceId'],
            ],
        )

        instance = instances['Reservations'][0]['Instances'][0]

        task_record = {
            'taskArn': task['taskArn'],
            'taskId': task['taskArn'].split('/')[1],
            'service': task['group'].split(":")[1],
            'lastStatus': task['lastStatus'],
            'desiredStatus': task['desiredStatus'],
            'instanceID': container_instance['ec2InstanceId'],
            'instanceIP': instance['PrivateIpAddress']
        }
        task_record['containers'] = []
        for container in task['containers']:
            task_record['containers'].append({
                "name": container['name'],
                "lastStatus": container['lastStatus'],
            })

        tasks_records.append(task_record)

    return tasks_records

@click.group()
@click_log.simple_verbosity_option(logger)
@click.option('--region', prompt='AWS Region (e.g us-west-2)')
@click.option('--cluster', prompt='ECS cluster name (e.g www-cluster)')
@click.option('--project-name', prompt='Project name (e.g. www)')
@click.option('--env', prompt='Environment name (dev/prod)')
@click.option('--component', help='Component to operate with. Leave empty if main or the only component in the deployment (e.g reverse_proxy, app)', default='')
@click.option('--ecs-params', default='./ecs-params.yml')
@click.option('--is-blue-green/--is-not-blue-green', default=True)
@click.option('--populate-target-env/--no-populate-target-env', default=True)
@click.option('--target-color', help='Specify target color for blue-green actions or leave default to use ALB status as context (default: context)', default='context')
@click.pass_context
def cli(ctx, region, cluster, project_name, env, component, ecs_params, is_blue_green, populate_target_env, target_color):
    ctx.obj['region'] = region
    ctx.obj['cluster'] = cluster
    ctx.obj['project-name'] = project_name
    ctx.obj['env'] = env
    ctx.obj['component'] = component
    ctx.obj['ecs-params'] = ecs_params
    ctx.obj['is-blue-green'] = is_blue_green
    ctx.obj['populate-target-env'] = populate_target_env

    # Assume ELB name from project name and environment.
    ctx.obj['elb-name'] = get_elb_name(project_name, env)

    ctx.obj['current-color'] = get_current_color(ctx.obj['elb-name'], ctx.obj['is-blue-green'])
    if target_color == 'context':
        ctx.obj['target-color'] = get_target_color_from_context(ctx)
    else:
        ctx.obj['target-color'] = target_color
    pass


@cli.command()
@click.pass_context
def promote(ctx):
    if not ctx.obj['is-blue-green']:
        logger.error('Promote only works in blue-green environment')
        sys.exit(1)

    try:
        logger.info(f"Current color: {ctx.obj['current-color']}")
        swaptargetgroups(ctx.obj['elb-name'])
        logger.info('Swap successful')
        new_color = get_current_color(ctx.obj['elb-name'], ctx.obj['is-blue-green'])
        logger.info(f"New color: {new_color}")

    except Exception as e:
        logger.error('Swap failed due to exception.')
        click.echo(e)
        traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.argument('version')
@click.option('--initial-scale', default='1')
@click.option('--deployment-max-percent', default='200')
@click.option('--deployment-min-healthy-percent', default='50')
@click.option('--attach-alb/--no-attach-alb', default=True)
@click.option('--alb-container-name', default='web')
@click.option('--alb-container-port', default='80')
@click.option('--health-check-grace-period', default='10')
@click.option('--timeout', default='10')
@click.pass_context
def deploy(ctx, version, initial_scale, deployment_max_percent, deployment_min_healthy_percent, attach_alb, alb_container_name, alb_container_port, health_check_grace_period, timeout):
    target_service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['component'], ctx.obj['target-color'])
    if not get_service(ctx.obj['cluster'], target_service_name):
        load_balancer_options = ''
        if attach_alb:
            tg_arn = get_target_group_arn_by_color(ctx.obj['elb-name'], ctx.obj['target-color'])
            load_balancer_options = f"--target-group-arn {tg_arn} --container-name {alb_container_name} --container-port {alb_container_port} --health-check-grace-period {health_check_grace_period} --role ecs-service"

        # Deploy initial service and attach it to the load balancer target group.
        run_service_command(ctx, f"service up --deployment-max-percent={deployment_max_percent} --deployment-min-healthy-percent={deployment_min_healthy_percent} --timeout={timeout} {load_balancer_options}", {"VERSION": version})
        # Scale to the desired service size.
        run_service_command(ctx, f"service scale --timeout={timeout} {initial_scale}")
    else:
        run_service_command(ctx, f"service up --deployment-max-percent={deployment_max_percent} --deployment-min-healthy-percent={deployment_min_healthy_percent} --timeout={timeout}", {"VERSION": version})

    # TODO: Implement target group health checking


@cli.command()
@click.pass_context
def stop(ctx):
    run_service_command(ctx, "service stop")


@cli.command()
@click.pass_context
def remove(ctx):
    run_service_command(ctx, f"service rm")


@cli.command()
@click.argument('replicas')
@click.pass_context
def scale(ctx, replicas):
    run_service_command(ctx, f"service scale {replicas}")


@cli.command()
@click.argument('container_name')
@click.argument('command')
@click.option('--ssh-options', '-o', multiple=True, help="Specify ssh options in the same format as SSH command supports. "
                                                         "You can specify this option multiple times. \n"
                                                         "Example: deploy.py exec --ssh-options StrictHostKeyChecking=no cli ls")
@click.pass_context
def exec(ctx, container_name, command, ssh_options):
    ssh_options_str = ''
    if ssh_options:
        ssh_options_str = '-o ' + ' -o '.join(map( lambda x: '"' + x + '"', ssh_options))

    target_service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['component'], ctx.obj['target-color'])

    tasks_records = get_status_tasks_records(ctx, target_service_name)

    logger.debug("Tasks records:\n" + pformat(tasks_records))

    instance_ip = tasks_records[0]['instanceIP']
    command = command.replace("'", "'\\''")
    cmd = f"ssh {ssh_options_str} ec2-user@{instance_ip} 'docker exec -i $(docker ps -a -q -f name=ecs-'{target_service_name}' -f label=com.amazonaws.ecs.container-name='{container_name}' | head -n 1 ) {command}'"
    run_cli_command(ctx, cmd)

@cli.command(name="get-target-color")
@click.pass_context
def get_target_color(ctx):
    click.echo(ctx.obj['target-color'], nl=False)

@cli.command(name="wait-for-services")
@click.option("--service-name", "-s", multiple=True)
@click.pass_context
def wait_for_services(ctx, service_name):
    try:
        waiter = ecsclient.get_waiter('services_stable')
        waiter.wait(
            cluster=ctx.obj['cluster'],
            services=service_name,
            WaiterConfig={
                'Delay': 5,
                'MaxAttempts': 40
            }
        )
        logger.info("Requested services are up and running")
    except WaiterError:
        logger.error('Requested services (one or more) either are not stable or do not exist')
        sys.exit(1)


@cli.command()
@click.option('--versioned-container-name', prompt='Which container to use to identify artifact version? (e.g. web)')
@click.pass_context
def status(ctx, versioned_container_name):
    ctx.obj['versioned-container-name'] = versioned_container_name
    elb_dns = ''
    try:
        response = elbclient.describe_load_balancers(
            Names=[
                ctx.obj['elb-name'],
            ],
        )

        elb_dns = response['LoadBalancers'][0]['DNSName']
    except:
        pass

    services_records = []
    tasks_records = []

    if ctx.obj['is-blue-green']:
        colors = COLORS
    else:
        colors = [DEFAULT_COLOR]

    for color in colors:
        service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['component'], color)
        service = get_service(ctx.obj['cluster'], service_name)
        if not service:
            continue

        task_definition = ecsclient.describe_task_definition(
            taskDefinition=service['taskDefinition']
        )

        version = "unknown"
        for container in task_definition['taskDefinition']['containerDefinitions']:
            # TODO: Make 'web' container name configurable.
            if (container['name'] == ctx.obj['versioned-container-name']):
                version = container['image'].split(":")[1]

        services_records.append({
            "name": service["serviceName"],
            "desiredCount": service["desiredCount"],
            "runningCount": service["runningCount"],
            "version": version,
        })

        tasks_records += get_status_tasks_records(ctx, service_name)

    print("Current active deployment: " + ctx.obj['current-color'])
    print("ELB DNS: " + elb_dns)

    print()
    format_string = "{:<30} {:<13} {:<13} {:<15}"

    print(format_string.format('Service', 'Desired Count', 'Running Count', 'Version'))
    for v in services_records:
        print(format_string.format(v['name'], v['desiredCount'], v['runningCount'], v['version']))

    print()
    format_string = "{:<38} {:<38} {:<11} {:<14} {:<13} {:<20}"

    print(format_string.format('Task ID', 'Service', 'Last Status', 'Desired Status', 'Instance IP', 'Container Status'))
    for v in tasks_records:
        container_status = []
        for container in v['containers']:
            container_status.append(container['name'] + ':' + container['lastStatus'])
        print(format_string.format(v['taskId'], v['service'], v['lastStatus'], v['desiredStatus'], v['instanceIP'], "; ".join(container_status)))


if __name__ == "__main__":
    cli(obj={})
