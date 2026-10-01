"""一个故意包含若干问题的示例模块，用于演示 Agent 的审查能力。"""


def divide(a, b):
    return a / b


def average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def read_config(path="/etc/app/config.yaml"):
    with open(path) as f:   # 未指定 encoding，且默认路径硬编码
        return f.read()


class UserCache:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data[key]      # 键不存在时直接 KeyError

    def put(self, key, value):
        self.data[key] = value
