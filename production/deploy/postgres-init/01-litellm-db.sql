-- postgres 镜像首次初始化时执行（/docker-entrypoint-initdb.d）：给 LiteLLM proxy 单独一个库（虚拟 key、预算、花费记录）
CREATE DATABASE litellm;
