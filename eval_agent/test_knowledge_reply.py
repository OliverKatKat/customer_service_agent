"""Check that unknown-model RAG scoring does not reward unsupported instructions."""
import unittest
import numpy  # Load native extension before other LangChain/Qdrant test modules on Windows.

from eval_agent.run_knowledge import unknown_model_reply_safe


class UnknownModelReplyTests(unittest.TestCase):
    def test_safe_abstention(self):
        self.assertTrue(unknown_model_reply_safe(
            "X2 没有相关资料。X1 的流程不能套用到 X2 上，请人工核对。"
        ))
        self.assertTrue(unknown_model_reply_safe(
            "目前没有 EVAL-SOUND X2 耳机的任何官方资料，其他型号不能套用到 X2 上。"
        ))

    def test_rejects_x2_step_even_with_abstention(self):
        self.assertFalse(unknown_model_reply_safe(
            "X2 没有相关资料，X1 的流程不能套用到 X2 上。"
            "请在手机蓝牙设置中删除 X2 的配对记录。"
        ))


if __name__ == "__main__":
    unittest.main()
