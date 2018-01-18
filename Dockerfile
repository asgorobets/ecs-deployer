FROM python:3-alpine

WORKDIR /usr/src/app

ADD https://s3.amazonaws.com/amazon-ecs-cli/ecs-cli-linux-amd64-latest /usr/local/bin/ecs-cli
RUN chmod 755 /usr/local/bin/ecs-cli

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY deploy.py /

ENTRYPOINT [ "python", "/deploy.py" ]
