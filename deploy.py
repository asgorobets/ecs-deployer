from __future__ import print_function

import os
import boto3
import traceback
import click
import subprocess

DEFAULT_COLOR = "blue"
OTHER_COLOR = "green"
COLORS = [DEFAULT_COLOR, OTHER_COLOR]

elbclient = boto3.client('elbv2')
ecsclient = boto3.client('ecs')
ec2client = boto3.client('ec2')

def get_elb_arn(elbname):
    try:
        elbresponse = elbclient.describe_load_balancers(Names=[elbname])
        return elbresponse['LoadBalancers'][0]['LoadBalancerArn']

    except Exception as e:
        print(
            f"""
            Requested ELB could not be found: {elbname}
            Make sure the ELB is created using Terraform template
            """
        )


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
        if x['Priority'] == '1':
            livetargetgroup = x['Actions'][0]['TargetGroupArn']
            liverulearn = x['RuleArn']

    betatgresponse = elbclient.describe_rules(ListenerArn=betalistenerarn)

    for x in betatgresponse['Rules']:
        if x['Priority'] == '1':
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

    modify_tags(state['live']['target-group-arn'],"IsProduction","False")
    modify_tags(state['beta']['target-group-arn'], "IsProduction", "True")


def modify_tags(arn,tagkey,tagvalue):
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


def get_service_name(project_name, env, color):
    return f"{project_name}-{env}-{color}"


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

    live_service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['current-color'])
    live_service = get_service(ctx.obj['cluster'], live_service_name)

    # Target the inactive color in case that live_service exists, otherwise target live color for initialization.
    return get_opposite_color(ctx.obj['current-color']) if live_service else ctx.obj['current-color']


def run_service_command(ctx, command, version="none"):
    target_service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['target-color'])
    cmd = f"ecs-cli compose --project-name {target_service_name} --cluster {ctx.obj['cluster']} --region {ctx.obj['region']} " + command

    click.secho(f"VERSION={version} {cmd}", fg='green')
    subprocess.run(cmd, shell=True, env=dict(os.environ, VERSION=version)).check_returncode()


@click.group()
@click.option('--region', prompt='AWS Region (e.g us-west-2)')
@click.option('--cluster', prompt='ECS cluster name (e.g www-cluster)')
@click.option('--project-name', prompt='Project name (e.g. www)')
@click.option('--env', prompt='Environment name (dev/prod)')
@click.option('--is-blue-green/--is-not-blue-green', default=True)
@click.option('--initial-scale', default="2")
@click.option('--debug', default=False)
@click.pass_context
def cli(ctx, region, cluster, project_name, env, is_blue_green, initial_scale, debug):
    ctx.obj['region'] = region
    ctx.obj['cluster'] = cluster
    ctx.obj['project-name'] = project_name
    ctx.obj['env'] = env
    ctx.obj['is-blue-green'] = is_blue_green
    ctx.obj['initial-scale'] = initial_scale
    ctx.obj['debug'] = debug

    # Assume ELB name from project name and environment.
    ctx.obj['elb-name'] = get_elb_name(project_name, env)

    ctx.obj['current-color'] = get_current_color(ctx.obj['elb-name'], ctx.obj['is-blue-green'])
    ctx.obj['target-color'] = get_target_color_from_context(ctx)
    pass


@cli.command()
@click.pass_context
def promote(ctx):
    if not ctx.obj['is-blue-green']:
        click.echo('Promote only works in blue-green environment')
        return
    try:
        click.echo(f"Current color: {ctx.obj['current-color']}")
        swaptargetgroups(ctx.obj['elb-name'])
        click.echo('Swap successful')
        new_color = get_current_color(ctx.obj['elb-name'], ctx.obj['is-blue-green'])
        click.echo(f"New color: {new_color}")

    except Exception as e:
        click.echo('Swap failed due to exception.')
        click.echo(e)
        traceback.print_exc()


@cli.command()
@click.argument('version')
@click.pass_context
def deploy(ctx, version):
    target_service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], ctx.obj['target-color'])
    if not get_service(ctx.obj['cluster'], target_service_name):
        tg_arn = get_target_group_arn_by_color(ctx.obj['elb-name'], ctx.obj['target-color'])

        # Deploy initial service and attach it to the load balancer target group.
        run_service_command(ctx, f"service up --target-group-arn {tg_arn} --container-name web --container-port 80 --role ecs-service", version)
        # Scale to the desired service size.
        run_service_command(ctx, f"service scale {ctx.obj['initial-scale']}")
    else:
        run_service_command(ctx, f"service up --deployment-max-percent=100 --deployment-min-healthy-percent=0", version)

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
@click.pass_context
def status(ctx):
    response = elbclient.describe_load_balancers(
        Names=[
            ctx.obj['elb-name'],
        ],
    )

    elb_dns = response['LoadBalancers'][0]['DNSName']

    services_records = []
    tasks_records = []

    if ctx.obj['is-blue-green']:
        colors = COLORS
    else:
        colors = [DEFAULT_COLOR]

    for color in colors:
        service_name = get_service_name(ctx.obj['project-name'], ctx.obj['env'], color)
        service = get_service(ctx.obj['cluster'], service_name)

        task_definition = ecsclient.describe_task_definition(
            taskDefinition=service['taskDefinition']
        )

        version = "unknown"
        for container in task_definition['taskDefinition']['containerDefinitions']:
            # TODO: Make 'web' container name configurable.
            if (container['name'] == "web"):
                version = container['image'].split(":")[1]

        services_records.append({
            "name": service["serviceName"],
            "desiredCount": service["desiredCount"],
            "runningCount": service["runningCount"],
            "version": version,
        })
    
        response = ecsclient.list_tasks(
            cluster=ctx.obj['cluster'],
            serviceName=service_name,
        )

        task_arns = response['taskArns']

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
