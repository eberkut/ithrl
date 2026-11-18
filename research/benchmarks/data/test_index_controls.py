# These independent reference checks run with the standard-library unittest runner.
# ruff: noqa: PT009
import unittest

from django_migration_linter.sql_analyser import analyse_sql_statements


class IndexControls(unittest.TestCase):
    def test_index_table_relationships(self):
        create_film = 'CREATE TABLE "films" ("title" text);'
        create_archive = 'CREATE TABLE "archive" ("title" text);'
        index_film = 'CREATE INDEX ON "films" ((lower("title")));'
        index_archive = 'CREATE INDEX ON "archive" ((lower("title")));'
        cases = {
            "new-table": ([create_film, index_film], False),
            "different-table": ([create_film, index_archive], True),
            "existing-table": ([index_film], True),
            "two-new-tables": ([create_film, create_archive, index_film, index_archive], False),
            "new-first-existing-second": ([create_film, index_film, index_archive], True),
            "existing-first-new-second": ([create_film, index_archive, index_film], True),
            "concurrent": (['CREATE INDEX CONCURRENTLY ON "archive" ((lower("title")));'], False),
            "two-existing-tables": ([index_film, index_archive], True),
            "no-space-existing-table": (['CREATE INDEX title_idx ON "films"("title");'], True),
            "no-space-new-table": (
                [create_film, 'CREATE INDEX title_idx ON "films"("title");'],
                False,
            ),
            "unquoted-prefix-is-not-table": (
                [
                    "CREATE TABLE films_archive (title text);",
                    "CREATE INDEX title_idx ON films (title);",
                ],
                True,
            ),
            "unquoted-same-table": (
                ["CREATE TABLE films (title text);", "CREATE INDEX title_idx ON films(title);"],
                False,
            ),
        }
        cases["leading-whitespace-existing"] = (
            ['\n  CREATE INDEX title_idx ON "films"("title");'],
            True,
        )
        cases["leading-whitespace-new"] = (
            [
                '\n CREATE TABLE "films" ("title" text);',
                '\n CREATE INDEX title_idx ON "films"("title");',
            ],
            False,
        )
        for name, (statements, expected) in cases.items():
            with self.subTest(name=name):
                _, _, warnings = analyse_sql_statements(statements, "postgresql")
                actual = any(item["code"] == "CREATE_INDEX" for item in warnings)
                self.assertEqual(actual, expected)
