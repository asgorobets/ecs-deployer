from __future__ import print_function

import os
import boto3
import traceback
import click
import subprocess
import yaml

elbclient = boto3.client('elbv2')

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

    cmd = f"ecs-cli compose --project-name {config['project-name']}-{color} --file={config['compose-file']} --cluster {config['cluster']} --region {config['region']} service up --deployment-max-percent=100 --deployment-min-healthy-percent=0"
    click.secho(f"VERSION={version} {cmd}", fg='green')
    subprocess.run(cmd, shell=True, env=dict(os.environ, VERSION=version)).check_returncode()

    # TODO: Implement target group health checking

def get_current_color():
    elbname = config['elb-name']
    state = get_target_groups(elbname)

    for color in ['blue', 'green']:
        if state['live']['target-group-arn'] == config[color]['target-group-arn']:
            return color

    raise RuntimeError('Live environment color identification failed')


if __name__ == "__main__":
    cli()
