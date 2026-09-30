FROM public.ecr.aws/lambda/python:3.14

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv

COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --no-cache -r /tmp/requirements.txt --target "${LAMBDA_TASK_ROOT}"

COPY *.py ${LAMBDA_TASK_ROOT}/

CMD ["app.lambda_handler"]
