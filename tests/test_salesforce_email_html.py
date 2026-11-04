import unittest

from salesforce_email_html import clean_email_html


class EmailMetadataTests(unittest.TestCase):
    def test_removes_pasted_metadata_preserving_rich_text_and_placeholders(self):
        source = (
            '<!doctype html>\n<html><body>\n<p data-pm-slice="1 1 []" style="color:#123456">'
            '<strong data-start="4">Hello &rsquo; [STOCK]</strong></p>'
            '<a href="https://example.test/?a=1&amp;b=2" data-end="12">[PRODUCT]</a>'
            '<img data-editor-id="image-1" src="https://example.test/logo.png" alt="Logo" />'
            '</body></html>\n'
        )
        expected = source
        for attribute in (' data-pm-slice="1 1 []"', ' data-start="4"', ' data-end="12"',
                          ' data-editor-id="image-1"'):
            expected = expected.replace(attribute, '')
        self.assertEqual(clean_email_html(source), expected)
        self.assertEqual(clean_email_html(expected), expected)

    def test_attribute_case_values_duplicates_and_line_endings(self):
        source = "<P\r\n DATA-PM-SLICE='1 > 0 []' data-start=0 data-pm-slice=\"2\" data-only>Hi</P>"
        self.assertEqual(clean_email_html(source), '<P>Hi</P>')
        self.assertEqual(clean_email_html('<p data-a="" data-b />'), '<p />')
        self.assertEqual(clean_email_html('<p data-a=unquoted>Hi</p>'), '<p>Hi</p>')

    def test_attribute_values_and_text_are_never_mistaken_for_metadata(self):
        source = (
            '<!-- <p data-pm-slice="keep comment"> -->'
            '<style>p:before{content:"data-pm-slice"}</style>'
            '<p title="data-pm-slice=\'keep > literal\'" style="font-size:24px">'
            'Explain data-pm-slice and &lt;p data-pm-slice="text"&gt;.</p>'
        )
        self.assertEqual(clean_email_html(source), source)

    def test_removal_does_not_merge_a_tag_with_adjacent_attributes(self):
        result = clean_email_html('<p data-pm-slice="1"class="kept" data-start="2">Hi</p>')
        self.assertEqual(result, '<p class="kept">Hi</p>')

    def test_all_element_types_and_quoted_angles_are_handled(self):
        source = '<div data-a="1"><p title="a > b" data-b="2"><br data-c="3"/>Hi</p></div>'
        self.assertEqual(clean_email_html(source), '<div><p title="a > b"><br/>Hi</p></div>')

    def test_remaining_attributes_keep_their_exact_original_spelling(self):
        source = '<P CLASS=Example style=\'font-size:14px\' align="left">Caf\u00e9 &amp; [REQUEST]</P>\r\n'
        self.assertEqual(clean_email_html(source), source)


if __name__ == '__main__':
    unittest.main()
