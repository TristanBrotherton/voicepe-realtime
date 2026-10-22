import unittest

from app.action_gate import ActionGate, EntityDirectory
from app.list_names import resolve_list_name

LISTS = ["Films list", "Bakery", "Coupons", "Garden Centre", "Packing list", "Tini", "DIY",
         "Shopping List", "Annie’s ", "Hardware Store"]


class ResolveListName(unittest.TestCase):
    def test_misheard_names_reach_the_closest_list(self):
        for spoken, real in (("teenie", "Tini"), ("Teenie list", "Tini"), ("tinny", "Tini"),
                             ("the shopping list", "Shopping List"), ("annies", "Annie’s "),
                             ("Annie's", "Annie’s "), ("film list", "Films list"),
                             ("packing", "Packing list"), ("coupon", "Coupons"), ("diy", "DIY")):
            self.assertEqual(resolve_list_name(spoken, LISTS), real, spoken)

    def test_unrelated_or_unclear_names_are_left_alone(self):
        for spoken in ("groceries", "shop", "", "list"):
            self.assertIsNone(resolve_list_name(spoken, LISTS), spoken)
        self.assertIsNone(resolve_list_name("tom", ["Tim", "Tam"]), "two lists equally close")

    def test_a_list_of_guesses_resolves_only_to_one_list(self):
        self.assertEqual(resolve_list_name("Tini, Tinny, Tenny, Hardware", LISTS + ["Tenny"]), None,
                         "two different lists among the guesses")
        self.assertEqual(resolve_list_name("Tini, Tinny, Teeny, or Gardening", LISTS), "Tini")

    def test_an_exact_name_with_a_comma_is_not_split(self):
        self.assertEqual(resolve_list_name("Bed, Bath", ["Bed, Bath", "Bed"]), "Bed, Bath")


class ReconcileListName(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_get_the_real_list_name(self):
        async def states():
            return [{"entity_id": "todo.tini", "attributes": {"friendly_name": "Tini"}},
                    {"entity_id": "light.teenie", "attributes": {"friendly_name": "Teenie Lamp"}}]
        gate = ActionGate(directory=EntityDirectory(states))
        args = await gate.reconcile_arguments(
            "todo__HassListAddItem", {"item": "a fence post", "name": "teenie"})
        self.assertEqual(args, {"item": "a fence post", "name": "Tini"})
        args = await gate.reconcile_arguments("HassListCompleteItem", {"item": "milk", "name": "Tini"})
        self.assertEqual(args["name"], "Tini")
        args = await gate.reconcile_arguments("HassListAddItem", {"item": "milk", "name": "groceries"})
        self.assertEqual(args["name"], "groceries", "no close list: Home Assistant decides")
        args = await gate.reconcile_arguments("HassTurnOn", {"name": "teenie"})
        self.assertEqual(args["name"], "teenie", "only list tools are resolved against lists")


if __name__ == "__main__":
    unittest.main()
