FROM public.ecr.aws/lambda/python:3.12
# Same xgboost package and version as the v2 BQ1 job (3.2.0); --no-deps skips the optional CUDA/NCCL wheels (CPU inference only).
RUN pip install --no-cache-dir numpy==2.2.6 scipy==1.15.3 pandas==2.2.3 && \
    pip install --no-cache-dir --no-deps xgboost==3.2.0
COPY rt_core.py bootstrap_logic.py app.py offline_gate.py ${LAMBDA_TASK_ROOT}/
CMD ["app.handler"]
