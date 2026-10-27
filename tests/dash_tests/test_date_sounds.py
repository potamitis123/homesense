import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import pandas as pd

# Add the project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# Patch dash.register_page before importing the page module
with patch('dash.register_page') as mock_register_page:
    from ast_dash.pages import date_sounds

class TestDateSoundsPage(unittest.TestCase):

    def test_layout_renders(self):
        """
        Tests that the layout of the date_sounds page can be rendered without errors.
        """
        try:
            # The layout is a variable in date_sounds.py
            layout = date_sounds.layout
            self.assertIsNotNone(layout)
            self.assertTrue(hasattr(layout, 'children'))
        except Exception as e:
            self.fail(f"Rendering the layout of date_sounds.py failed with: {e}")

    @patch('ast_dash.pages.date_sounds.sqlite3.connect')
    @patch('ast_dash.pages.date_sounds.pd.read_sql_query')
    def test_get_db_data_for_date_with_data(self, mock_read_sql, mock_connect):
        """
        Tests the get_db_data_for_date function with mock DB data.
        """
        # Mock DB Return Data
        mock_df = pd.DataFrame({
            'sound1': ['Dog', 'Cat'],
            'sound2': ['Cat', None],
            'date': ['2026-01-14 10:00:00', '2026-01-14 11:00:00'],
            'device_name': ['box3-1', 'box3-1']
        })
        mock_read_sql.return_value = mock_df
        
        # Call the function
        result_df = date_sounds.get_db_data_for_date('2026-01-14')

        # Assertions
        self.assertIsInstance(result_df, pd.DataFrame)
        self.assertFalse(result_df.empty)
        self.assertIn('sound1', result_df.columns)
        self.assertIn('count', result_df.columns)
        
        # Verify processing (Dog: 1, Cat: 2)
        # Note: logic combines sound1 and sound2, filters None
        # Dog at 10:00, Cat at 10:00, Cat at 11:00
        cat_row = result_df[result_df['sound1'] == 'Cat']
        self.assertFalse(cat_row.empty)

    @patch('ast_dash.pages.date_sounds.sqlite3.connect')
    @patch('ast_dash.pages.date_sounds.pd.read_sql_query')
    def test_get_db_data_for_date_filters_by_sentinel(self, mock_read_sql, mock_connect):
        """Only events from the selected sentinel are counted."""
        mock_df = pd.DataFrame({
            'sound1': ['Dog', 'Cat', 'Bird'],
            'sound2': [None, None, None],
            'date': ['2026-01-14 10:00:00', '2026-01-14 11:00:00', '2026-01-14 12:00:00'],
            'device_name': ['box3-1', 'p4-sentinel-1', 'box3-1']
        })
        mock_read_sql.return_value = mock_df

        result_df = date_sounds.get_db_data_for_date('2026-01-14', device='box3-1')

        self.assertFalse(result_df.empty)
        sounds = set(result_df['sound1'].tolist())
        self.assertEqual(sounds, {'Dog', 'Bird'})
        self.assertEqual(int(result_df['count'].sum()), 2)

    @patch('ast_dash.pages.date_sounds.sqlite3.connect')
    @patch('ast_dash.pages.date_sounds.pd.read_sql_query')
    def test_get_db_data_for_date_all_keeps_every_device(self, mock_read_sql, mock_connect):
        """device='all' preserves current all-sentinels behavior."""
        mock_df = pd.DataFrame({
            'sound1': ['Dog', 'Cat'],
            'sound2': [None, None],
            'date': ['2026-01-14 10:00:00', '2026-01-14 11:00:00'],
            'device_name': ['box3-1', 'p4-sentinel-1']
        })
        mock_read_sql.return_value = mock_df

        result_df = date_sounds.get_db_data_for_date('2026-01-14', device='all')

        self.assertEqual(set(result_df['sound1'].tolist()), {'Dog', 'Cat'})
        
    @patch('ast_dash.pages.date_sounds.sqlite3.connect')
    @patch('ast_dash.pages.date_sounds.pd.read_sql_query')
    def test_get_db_data_for_date_no_data(self, mock_read_sql, mock_connect):
        """
        Tests the get_db_data_for_date function when DB returns empty.
        """
        mock_read_sql.return_value = pd.DataFrame()

        result_df = date_sounds.get_db_data_for_date('2026-01-15')

        self.assertTrue(result_df.empty)

if __name__ == '__main__':
    unittest.main()
