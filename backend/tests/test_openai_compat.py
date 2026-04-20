import unittest

from backend.openai_compat import parse_chat_completion_payload


class ParseChatCompletionPayloadTests(unittest.TestCase):
    def test_parse_standard_json_response(self):
        data = parse_chat_completion_payload(
            """
            {
              "choices": [
                {
                  "message": {
                    "role": "assistant",
                    "content": "hello"
                  }
                }
              ],
              "usage": {
                "total_tokens": 12
              }
            }
            """
        )

        self.assertEqual(data["choices"][0]["message"]["content"], "hello")
        self.assertEqual(data["usage"]["total_tokens"], 12)

    def test_parse_sse_chunk_response(self):
        data = parse_chat_completion_payload(
            """
            data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"分析"}}]}

            data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"完成"}}],"usage":{"total_tokens":34}}

            data: [DONE]
            """
        )

        self.assertEqual(data["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(data["choices"][0]["message"]["content"], "分析完成")
        self.assertEqual(data["usage"]["total_tokens"], 34)

    def test_parse_sse_full_message_response(self):
        data = parse_chat_completion_payload(
            """
            data: {"id":"chatcmpl-2","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"完整结果"}}]}
            data: [DONE]
            """
        )

        self.assertEqual(data["choices"][0]["message"]["content"], "完整结果")


if __name__ == "__main__":
    unittest.main()
