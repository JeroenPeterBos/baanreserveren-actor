import re


def to_snake_case(s):
    # Replace all non-word characters (except for '_') with spaces
    s = re.sub(r"[\W_]+", " ", s)
    # Convert CamelCase to camel_case
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()
    # Replace multiple spaces with a single underscore
    s = re.sub(r"\s+", "_", s)
    return s.strip("_")


if __name__ == "__main__":
    print(to_snake_case("CamelCase"))
    print(to_snake_case("CamelCaseWithABBR"))
    print(to_snake_case("CamelCaseWith123"))
    print(to_snake_case("CamelCaseWith_123"))
    print(to_snake_case("CamelCaseWith-123"))
    print(to_snake_case("Hey there!"))
