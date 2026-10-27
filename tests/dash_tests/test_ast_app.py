import os
import shutil
import unittest
from unittest.mock import patch, MagicMock

# Adjust the path to import the module from the parent directory

import sys
import os
# Add the project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from ast_dash import ast_app
from ast_sql.ast_sql_lite import Ast_Sql_lite
from misc import hash_password


class TestAstApp(unittest.TestCase):

    def setUp(self):
        # Create a dummy assets folder path for testing
        self.test_assets_path = 'test_assets'
        # Ensure it does not exist before each test
        if os.path.exists(self.test_assets_path):
            shutil.rmtree(self.test_assets_path)

    def tearDown(self):
        # Clean up the dummy assets folder after each test
        if os.path.exists(self.test_assets_path):
            shutil.rmtree(self.test_assets_path)

    @patch('os.path.exists')
    @patch('os.makedirs')
    def test_create_assets_folder_creates_folder(self, mock_makedirs, mock_exists):
        # Mock that the folder does not exist
        mock_exists.return_value = False
        ast_app.create_assets_folder()
        # Assert that makedirs was called with the expected path
        mock_makedirs.assert_called_once_with('../assets')

    @patch('os.path.exists')
    @patch('os.makedirs')
    def test_create_assets_folder_handles_existing_folder(self, mock_makedirs, mock_exists):
        # Mock that the folder already exists
        mock_exists.return_value = True
        ast_app.create_assets_folder()
        # Assert that makedirs was NOT called
        mock_makedirs.assert_not_called()

    @patch('dash.page_registry', {
        'page1': {'name': 'Page 1', 'relative_path': '/page1'},
        'page2': {'name': 'Page 2', 'relative_path': '/page2'},
    })
    def test_MyNavBar_structure(self):
        navbar = ast_app.MyNavBar()
        # Check if it's a dbc.Navbar
        self.assertEqual(type(navbar).__name__, 'Navbar')
        self.assertEqual(navbar.color, 'primary')
        self.assertTrue(navbar.dark)

        # Check the children of the Navbar (should be a html.Div containing brand and nav)
        div_children = navbar.children
        self.assertEqual(type(div_children).__name__, 'Div')
        self.assertEqual(len(div_children.children), 2) # Brand and Nav

        brand_item = div_children.children[0]
        self.assertEqual(type(brand_item).__name__, 'NavbarBrand')
        # self.assertEqual(type(brand_item.children).__name__, 'NavbarBrand') # It IS the NavbarBrand
        # self.assertEqual(brand_item.children.children, 'Audio Surveillance') # Structure depends on icon implementation

        nav_component = div_children.children[1]
        self.assertEqual(type(nav_component).__name__, 'Nav')
        self.assertEqual(len(nav_component.children), 2) # Two NavItems for two pages

        # Check the first NavItem
        nav_item1 = nav_component.children[0]
        self.assertEqual(type(nav_item1).__name__, 'NavItem')
        self.assertEqual(type(nav_item1.children).__name__, 'NavLink')
        self.assertEqual(nav_item1.children.children, 'Page 1')
        self.assertEqual(nav_item1.children.href, '/page1')

        # Check the second NavItem
        nav_item2 = nav_component.children[1]
        self.assertEqual(type(nav_item2).__name__, 'NavItem')
        self.assertEqual(type(nav_item2.children).__name__, 'NavLink')
        self.assertEqual(nav_item2.children.children, 'Page 2')
        self.assertEqual(nav_item2.children.href, '/page2')

if __name__ == '__main__':
    unittest.main()
