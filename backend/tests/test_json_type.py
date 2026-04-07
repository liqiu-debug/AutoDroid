import warnings
import unittest

from pydantic import BaseModel

from backend.json_type import PydanticListType


class _StepModel(BaseModel):
    name: str
    order: int


class PydanticListTypeTests(unittest.TestCase):
    def test_bind_param_uses_warning_free_dump_for_pydantic_models(self):
        column_type = PydanticListType(_StepModel)
        items = [_StepModel(name="login", order=1)]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            payload = column_type.process_bind_param(items, dialect=None)

        self.assertEqual(payload, [{"name": "login", "order": 1}])
        self.assertEqual(caught, [])

    def test_result_value_uses_warning_free_validation_for_models(self):
        column_type = PydanticListType(_StepModel)
        raw_value = [{"name": "login", "order": 1}]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            payload = column_type.process_result_value(raw_value, dialect=None)

        self.assertEqual(len(payload), 1)
        self.assertIsInstance(payload[0], _StepModel)
        self.assertEqual(payload[0].name, "login")
        self.assertEqual(caught, [])

    def test_result_value_supports_primitive_lists(self):
        column_type = PydanticListType(str)
        payload = column_type.process_result_value(["smoke", "ios"], dialect=None)
        self.assertEqual(payload, ["smoke", "ios"])


if __name__ == "__main__":
    unittest.main()
