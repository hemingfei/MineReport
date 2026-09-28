-- postgres 初始化：zhparser 扩展 + 中文全文检索配置（zhcfg）
-- 由 abcfy2/zhparser 镜像的 docker-entrypoint-initdb.d 机制在首启时执行
CREATE EXTENSION IF NOT EXISTS zhparser;
CREATE TEXT SEARCH CONFIGURATION zhcfg (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION zhcfg ADD MAPPING FOR n,v,a,i,e,l WITH simple;
