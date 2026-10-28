"""Construct clients without contacting optional services during API import."""
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams
from langchain_qdrant import QdrantVectorStore
from langchain_openai import OpenAIEmbeddings
from app.core.config import settings

# 1. 初始化统一的 Embedding 引擎
embeddings = OpenAIEmbeddings(
    openai_api_key=settings.DASHSCOPE_API_KEY, 
    openai_api_base=settings.EMBEDDING_API_BASE,
    model=settings.EMBEDDING_MODEL_NAME,
    check_embedding_ctx_length=False 
)

# 2. 使用原生客户端连接 Qdrant
qdrant_client = QdrantClient(url=settings.QDRANT_URL, trust_env=False, check_compatibility=False)

# 3. 🛡️ 自动建表逻辑：长期记忆库
# 阿里云 text-embedding-v4 的标准维度是 1024，距离算法用 COSINE (余弦相似度)
def initialize_memory_collection():
    """Initialize on Agent use, keeping manual case and review routes available."""
    name = "user_longterm_memory"
    if not qdrant_client.collection_exists(name):
        try:
            qdrant_client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
            )
        except Exception:
            # Concurrent first requests may race to create the same collection.
            if not qdrant_client.collection_exists(name):
                raise

# 4. 知识库 1：公司规章制度静态库
qdrant_company = QdrantVectorStore(
    client=qdrant_client,
    collection_name=settings.COMPANY_KNOWLEDGE_COLLECTION,
    embedding=embeddings,
    validate_collection_config=False,
)
# 导出公司规章 retriever 供其他模块使用

# 5. 知识库 2：用户专属长期情景记忆动态库
qdrant_longterm = QdrantVectorStore(
    client=qdrant_client,
    collection_name="user_longterm_memory",
    embedding=embeddings,
    validate_collection_config=False,
)
