ARG PYTHON_VERSION=3.9

# contains AWS Lambda runtime
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION}

ARG TOPIC_NAME=youtube-pubsubhubbub

COPY app.py ${LAMBDA_TASK_ROOT}
COPY requirements.txt  .

ENV TOPIC_NAME=$TOPIC_NAME

RUN  pip3 install -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

CMD [ "app.lambda_handler" ]

