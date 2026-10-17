from __future__ import annotations

import unittest

from jevguard.client import TypeSafeError, check_answers
from jevguard.questions import DIMENSIONS, build_questions


def valid_answers() -> dict:
    answers = {name: {"noul": 0.1} for name in DIMENSIONS}
    answers["verdict"] = {"choice": "safe", "probabilities": {"safe": 0.9, "danger": 0.1}, "confidence": 0.8}
    return answers


class CheckAnswersTests(unittest.TestCase):
    def test_valid_reply_passes(self) -> None:
        check_answers(valid_answers(), build_questions())

    def test_missing_answers_object_fails(self) -> None:
        with self.assertRaises(TypeSafeError):
            check_answers(None, build_questions())

    def test_missing_factor_fails(self) -> None:
        answers = valid_answers()
        del answers["destroys"]
        with self.assertRaises(TypeSafeError):
            check_answers(answers, build_questions())

    def test_missing_verdict_fails(self) -> None:
        answers = valid_answers()
        del answers["verdict"]
        with self.assertRaises(TypeSafeError):
            check_answers(answers, build_questions())

    def test_out_of_range_noul_fails(self) -> None:
        answers = valid_answers()
        answers["escapes"] = {"noul": 1.7}
        with self.assertRaises(TypeSafeError):
            check_answers(answers, build_questions())

    def test_unknown_choice_option_fails(self) -> None:
        answers = valid_answers()
        answers["verdict"]["probabilities"] = {"fine": 1.0}
        with self.assertRaises(TypeSafeError):
            check_answers(answers, build_questions())

    def test_missing_confidence_fails(self) -> None:
        answers = valid_answers()
        del answers["verdict"]["confidence"]
        with self.assertRaises(TypeSafeError):
            check_answers(answers, build_questions())


if __name__ == "__main__":
    unittest.main()
