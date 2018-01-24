from __future__ import print_function

import os
import boto3
import traceback
import click
import subprocess
import yaml

elbclient = boto3.client('elbv2')
ecsclient = boto3.client('ecs')
ec2client = boto3.client('ec2')

def get_config():
    """
    Returns deploy script configuration
    """
    stream = open('config.yml', 'r')
    return yaml.load(stream)


# Set global config object.
# TODO: Refactor not to use global variable.
# TODO: Look into click_config package: https://github.com/EverythingMe/click-config
config = get_config()

def get_target_groups(elbname):
    """
    Returns target groups and listener rules info for live and beta deployment
    :param elbname: 
    :return: 
    """
    elbresponse = elbclient.describe_load_balancers(Names=[elbname])

    listeners = elbclient.describe_listeners(LoadBalancerArn=elbresponse['LoadBalancers'][0]['LoadBalancerArn'])
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

    print("Live=" + livetargetgroup)
    print("Beta=" + betatargetgroup)

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

    print(modifyOnBeta)

    modifyOnLive = elbclient.modify_rule(
        RuleArn=state['live']['elb-listener-rule-arn'],
        Actions=[
            {
                'Type': 'forward',
                'TargetGroupArn': state['beta']['target-group-arn']
            }
        ]
    )

    print(modifyOnLive)
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


def get_service_name(color):
    return f"{config['project-name']}-{color}"


def get_current_color():
    elbname = config['elb-name']
    state = get_target_groups(elbname)

    for color in ['blue', 'green']:
        if state['live']['target-group-arn'] == config[color]['target-group-arn']:
            return color

    raise RuntimeError('Live environment color identification failed')


@click.group()
@click.option('--debug', default=False)
def cli(debug):
    pass


@cli.command()
def promote():

    elbname = config['elb-name']

    try:
        click.echo("ELBNAME="+elbname)
        swaptargetgroups(elbname)

    except Exception as e:
        click.echo('Swap failed due to exception.')
        click.echo(e)
        traceback.print_exc()

@cli.command()
@click.argument('version')
def deploy(version):

    color = "green" if get_current_color() == "blue" else "blue"

    cmd = f"ecs-cli compose --project-name {get_service_name(color)} --file={config['compose-file']} --cluster {config['cluster']} --region {config['region']} service up --deployment-max-percent=100 --deployment-min-healthy-percent=0"
    click.secho(f"VERSION={version} {cmd}", fg='green')
    subprocess.run(cmd, shell=True, env=dict(os.environ, VERSION=version)).check_returncode()

    # TODO: Implement target group health checking


@cli.command()
def status():

    response = elbclient.describe_load_balancers(
        Names=[
            config['elb-name'],
        ],
    )

    elb_dns = response['LoadBalancers'][0]['DNSName']

    services_records = []
    tasks_records = []

    for color in ["blue", "green"]:
        response = ecsclient.describe_services(
            cluster=config['cluster'],
            services=[
                get_service_name(color),
            ]
        )

        service = response['services'][0]

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
            cluster=config['cluster'],
            serviceName=get_service_name(color),
        )
    
        task_arns = response['taskArns']

        tasks_list = ecsclient.describe_tasks(
            cluster=config['cluster'],
            tasks=task_arns
        )


        for task in tasks_list['tasks']:

            container_instances = ecsclient.describe_container_instances(
                cluster=config['cluster'],
                containerInstances=[
                    task['containerInstanceArn'],
                ]
            )

            containerInstance = container_instances['containerInstances'][0]

            instances = ec2client.describe_instances(
                InstanceIds=[
                    containerInstance['ec2InstanceId'],
                ],
            )

            instance = instances['Reservations'][0]['Instances'][0]

            task_record = {
                'taskArn': task['taskArn'],
                'taskId': task['taskArn'].split('/')[1],
                'service': task['group'].split(":")[1],
                'lastStatus': task['lastStatus'],
                'desiredStatus': task['desiredStatus'],
                'instanceID': containerInstance['ec2InstanceId'],
                'instanceIP': instance['PrivateIpAddress']
            }
            task_record['containers'] = []
            for container in task['containers']:
                task_record['containers'].append({
                    "name": container['name'],
                    "lastStatus": container['lastStatus'],
                })

            tasks_records.append(task_record)




    print("Current active deployment: " + get_current_color())
    print("ELB DNS: " + elb_dns)

    print()
    format_string = "{:<30} {:<13} {:<13} {:<15}"

    print(format_string.format('Service', 'Desired Count', 'Running Count', 'Version'))
    for v in services_records:
        print(format_string.format(v['name'], v['desiredCount'], v['runningCount'], v['version']))

    print()
    format_string = "{:<38} {:<26} {:<11} {:<14} {:<13} {:<20}"

    print(format_string.format('Task ID', 'Service', 'Last Status', 'Desired Status', 'Instance IP', 'Container Status'))
    for v in tasks_records:
        containerStatus = []
        for container in v['containers']:
            containerStatus.append(container['name'] + ':' + container['lastStatus'])
        print(format_string.format(v['taskId'], v['service'], v['lastStatus'], v['desiredStatus'], v['instanceIP'], "; ".join(containerStatus)))


if __name__ == "__main__":
    cli()
