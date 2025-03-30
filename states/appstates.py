from typing import List
from modals.instances import SearxInstanceStats


class AppState:

    def __init__(self):
        self.available_instances: List[SearxInstanceStats] = []

    def get_instances(self) -> List[SearxInstanceStats]:
        return self.available_instances

    def set_instances(self, instances: List[SearxInstanceStats]) -> None:
        self.available_instances = instances
