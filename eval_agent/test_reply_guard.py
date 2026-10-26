import unittest

from app.agent.reply_guard import (
    claims_refund_completion,
    discloses_pending_review,
    ground_pending_refund_reply,
)


class RefundCompletionClaimTests(unittest.TestCase):
    def test_detects_ambiguous_pending_review_wording(self):
        self.assertTrue(claims_refund_completion("您的退款申请我已经处理好了，已提交人工审核。"))
        self.assertTrue(claims_refund_completion("已为您处理完毕，当前仍在等待退款审核。"))

    def test_allows_explicit_pending_review_wording(self):
        self.assertFalse(claims_refund_completion("退款申请已提交人工审核，目前仍在等待审核。"))
        self.assertFalse(claims_refund_completion("审核批准前，订单退款状态不会更新。"))
        self.assertTrue(discloses_pending_review("申请已提交全额退款申请，目前正在人工审核中。"))
        self.assertTrue(discloses_pending_review("审核批准前，退款状态不会更新。"))

    def test_does_not_mistake_historical_status_for_new_completion(self):
        self.assertFalse(claims_refund_completion("系统记录该订单已退款，但无法确认实际到账。"))

    def test_collapses_duplicate_punctuation(self):
        reply = "退款申请我已经处理好了。请耐心等待人工审核。"
        grounded = ground_pending_refund_reply(reply)
        self.assertNotIn("。。", grounded)
        self.assertFalse(claims_refund_completion(grounded))

    def test_replaces_only_ambiguous_phrase_and_keeps_other_details(self):
        reply = "您的退款申请我已经处理好了：优惠券目前无法发放。"
        grounded = ground_pending_refund_reply(reply)
        self.assertIn("退款申请已提交人工审核", grounded)
        self.assertIn("优惠券目前无法发放", grounded)
        self.assertNotIn("。：", grounded)
        self.assertFalse(claims_refund_completion(grounded))
        processed = ground_pending_refund_reply("退款申请已经处理完毕：状态待审核。")
        self.assertTrue(processed.startswith("退款申请已提交人工审核"))
        self.assertFalse(processed.startswith("退款申请退款申请"))
        self.assertFalse(claims_refund_completion(processed))


if __name__ == "__main__":
    unittest.main()
