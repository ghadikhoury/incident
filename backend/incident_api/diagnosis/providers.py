"""Isolated model calls for incident diagnosis."""

import json
import time

from google import genai
from google.genai import errors, types


class BedrockProvider:
    def __init__(self, client, model_id: str, schema: dict):
        self.client = client
        self.model_id = model_id
        self.schema = schema

    def generate(self, system: str, prompt: str) -> str:
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1600, "temperature": 0},
            outputConfig={
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "name": "incident_diagnosis",
                            "description": "Incident diagnosis and safe suggestions",
                            "schema": json.dumps(self.schema),
                        }
                    },
                }
            },
        )
        if response.get("stopReason") != "end_turn":
            raise ValueError("Bedrock response did not finish")
        return "".join(block.get("text", "") for block in response["output"]["message"]["content"])


class GeminiProvider:
    def __init__(self, api_key: str, model_id: str, schema: dict, client=None):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for DIAGNOSIS_PROVIDER=gemini")
        self.client = client or genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=20_000)
        )
        self.model_id = model_id
        self.schema = schema

    def generate(self, system: str, prompt: str) -> str:
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model_id,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        response_json_schema=self.schema,
                        max_output_tokens=1600,
                        temperature=0,
                    ),
                )
                candidates = response.candidates or []
                if not candidates or str(candidates[0].finish_reason).split(".")[-1] != "STOP":
                    raise ValueError("Gemini response was refused or incomplete")
                if not response.text or not response.text.strip():
                    raise ValueError("Gemini response was empty")
                return response.text
            except errors.APIError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(min(2**attempt, 2))
        raise AssertionError("unreachable")
