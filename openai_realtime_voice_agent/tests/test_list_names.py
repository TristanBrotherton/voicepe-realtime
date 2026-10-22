import unittest

from app.action_gate import ActionGate, EntityDirectory
from app.list_names import resolve_list_name

LISTS = ["films list", "bakery", "coupons", "garden centre", "packing list", "tini", "diy",
         "shopping list", "annie’s ", "hardware store"]


class ResolveListName(unittest.TestCase):
    def test_misheard_names_reach_the_closest_list(self):
        for spoken, real in (("teenie", "tini"), ("teenie list", "tini"), ("tinny", "tini"),
                             ("the shopping list", "shopping list"), ("annies", "annie’s "),
                             ("annie's", "annie’s "), ("film list", "films list"),
                             ("packing", "packing list"), ("coupon", "coupons"), ("diy", "diy")):
            self.assertEqual(resolve_list_name(spoken, LISTS), real, spoken)

    def test_unrelated_or_unclear_names_are_left_alone(self):
        for spoken in ("groceries", "shop", "", "list"):
            self.assertIsNone(resolve_list_name(spoken, LISTS), spoken)
        self.assertIsNone(resolve_list_name("tom", ["tim", "tam"]), "two lists equally close")

    def test_a_list_of_guesses_resolves_only_to_one_list(self):
        self.assertEqual(resolve_list_name("tini, tinny, tenny, hardware", LISTS + ["tenny"]), None,
                         "two different lists among the guesses")
        self.assertEqual(resolve_list_name("tini, tinny, teeny, or gardening", LISTS), "tini")

    def test_an_exact_name_with_a_comma_is_not_split(self):
        self.assertEqual(resolve_list_name("bed, bath", ["bed, bath", "bed"]), "bed, bath")


class ReconcileListName(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_get_the_real_list_name(self):
        async def states():
            return [{"entity_id": "todo.tini", "attributes": {"friendly_name": "tini"}},
                    {"entity_id": "light.teenie", "attributes": {"friendly_name": "teenie lamp"}}]
        gate = ActionGate(directory=EntityDirectory(states))
        args = await gate.reconcile_arguments(
            "todo__HassListAddItem", {"item": "a fence post", "name": "teenie"})
        self.assertEqual(args, {"item": "a fence post", "name": "tini"})
        args = await gate.reconcile_arguments("HassListCompleteItem", {"item": "milk", "name": "tini"})
        self.assertEqual(args["name"], "tini")
        args = await gate.reconcile_arguments("HassListAddItem", {"item": "milk", "name": "groceries"})
        self.assertEqual(args["name"], "groceries", "no close list: Home Assistant decides")
        args = await gate.reconcile_arguments("HassTurnOn", {"name": "teenie"})
        self.assertEqual(args["name"], "teenie", "only list tools are resolved against lists")


if __name__ == "__main__":
    unittest.main()
