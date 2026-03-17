import unittest

from ai_server import normalize_diet, template_diet_consistent


class DietFilterTests(unittest.TestCase):
    def test_normalize_diet_aliases(self):
        self.assertEqual(normalize_diet("vegetarien"), "vegetarian")
        self.assertEqual(normalize_diet("poisson_uniquement"), "pescatarian")
        self.assertEqual(normalize_diet("vegan"), "vegan")

    def test_omnivore_does_not_accept_vegetarian_template(self):
        template = {
            "diet": "vegetarian",
            "composition": [
                {"diets": "vegetarian,omnivore"},
            ],
        }
        self.assertFalse(template_diet_consistent(template, "omnivore"))

    def test_vegetarian_template_with_vegetarian_ingredients_is_valid(self):
        template = {
            "diet": "vegetarian",
            "composition": [
                {"diets": "vegetarian,omnivore"},
                {"diets": "vegan,vegetarian,pescatarian,omnivore"},
            ],
        }
        self.assertTrue(template_diet_consistent(template, "vegetarian"))

    def test_vegetarian_template_rejected_if_one_ingredient_is_omnivore_only(self):
        template = {
            "diet": "vegetarian",
            "composition": [
                {"diets": "vegetarian,omnivore"},
                {"diets": "omnivore"},
            ],
        }
        self.assertFalse(template_diet_consistent(template, "vegetarian"))


if __name__ == "__main__":
    unittest.main()
