# AWS ECS Deploy script with Blue/Green support

## Architecture 

This script is used to orchestrate blue/green deployments of AWS ECS services using Docker Compose format and ECS CLI.

The blue/green swap is implemented using target groups and ALB listener rules, find the approach described in [this article](https://aws.amazon.com/blogs/compute/bluegreen-deployments-with-amazon-ecs/)

## Features
- Deploy specific color (blue/green) stack and attach service to ALB automatically if deploying first time
- Get next deployment target color to use in scriptin
- Swap target group listeners (aka promote inactive to active)
- Execute commands inside a container in ECS Task of your choice via SSH
- Waiter service that enables blocking script execution until a certain service reports as stable

## Building the image
1. Checkout the repo
2. Run `docker build -t ecs-deployer .`
3. Optionally push to your prefered Docker registry

## Usage
If you're using the build image, you'll need to run it as one-off container, here is an example:
```
docker run --rm \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION=us-west-2 \
  -i 
  ecs-deployer [command]
```
For simplicity we will refer to the script as shortened `./deploy.py`.

### Common usage scenarios:
```
# Get target color (opposite of production, or blue if none)
TARGET_COLOR=`./deploy.py --env dev --project-name www --cluster www --region us-west-2 get-target-color`

# Deploy to target color and attach service to ALB.
./deploy.py [...config omitted] \
  --target-color ${TARGET_COLOR} \
  deploy \
  --alb-container-name web \
  --alb-container-port 8080 \
  $VERSION

# Execute remote command in container via SSH
./deploy.py [...config omitted]
  --target-color ${TARGET_COLOR} \
  exec \
  ${SSH_OPTIONS} \
  cli \"sh -c 'export \\\$(cat /etc/secrets/vars.env | xargs) && robo drupal:update --copy-db'\"

# Swap blue with green
./deploy.py [...config omitted] \
  promote
  ```

## Contributions
Pull requests are very welcome

## Roadmap
- Allow fully custom load balancer names and target group names instead of reling on combination of [project], [env] and [color]

