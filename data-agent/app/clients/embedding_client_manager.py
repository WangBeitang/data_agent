import asyncio
from pathlib import Path
from typing import Optional

from app.conf.app_config import EmbeddingConfig, app_config
from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings


class EmbeddingClientManager:
    """
    生成向量数据的客户端器类
    """
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self.client: Optional[Embeddings] = None

    def _get_model_path(self) -> Path:
        model_path = Path(self.config.model_path)
        if not model_path.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            model_path = project_root / model_path
        model_path = model_path.resolve()
        if not model_path.is_dir():
            raise FileNotFoundError(f"Embedding 模型目录不存在: {model_path}")
        return model_path

    def init(self):
        model_path = self._get_model_path()
        self.client = HuggingFaceEmbeddings(
            model_name=str(model_path),
            model_kwargs={"device": self.config.device},
            encode_kwargs={
                "normalize_embeddings": self.config.normalize_embeddings,
            },
            show_progress=False,
        )

# 创建客户端管理器
embedding_client_manager = EmbeddingClientManager(app_config.embedding)

if __name__ == "__main__":
    async def test():
        embedding_client_manager.init()

        # 对指定文本进行向量化
        text = "hello world"
        result = embedding_client_manager.client.embed_query(text)
        print(result) # 包含1024个点数据的数组
        print(len(result))

    asyncio.run(test())
