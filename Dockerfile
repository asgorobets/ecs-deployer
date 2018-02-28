FROM python:3-alpine

WORKDIR /usr/src/app

RUN apk add --no-cache openssh-client
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ADD https://s3.amazonaws.com/amazon-ecs-cli/ecs-cli-linux-amd64-latest /usr/local/bin/ecs-cli
RUN chmod u+x /usr/local/bin/ecs-cli
COPY deploy.py /
COPY docker-entrypoint.sh /

ENTRYPOINT [ "/docker-entrypoint.sh" ]
