"""S&S page operations in an explicitly connected normal Chrome browser."""
import re
import time
from urllib.parse import quote

from ssactivewear_bridge import SxsSession, SxsBridgeError
import ssactivewear_ordering as sxs


class SxsExtensionBrowser:
    is_sxs_extension = True

    def __init__(self, order_id, session=None):
        self.session = session or SxsSession(order_id)
        self.order_id = str(order_id)
        self.current_url = sxs.BASE_URL + '/'
        self.owned_lines = None

    def call(self, operation, **payload):
        result = self.session.call(operation, payload)
        if not isinstance(result, dict) or not isinstance(result.get('url'), str) or not result['url']:
            raise SxsBridgeError('S&S browser returned an incomplete response. Review its tab before retrying.')
        self.current_url = result['url']
        sxs._require_site(self)
        return result

    def get(self, url):
        result = self.call('navigate', url=url)
        self.current_url = result['url']

    def ensure_login(self):
        self.get(sxs.CART_URL)
        state = self.call('health')
        if state.get('signed_in') is not True or state.get('cart_container') is not True:
            raise RuntimeError('Sign in to S&S normally in the connected browser, then run the extension browser check. No credentials are sent through the extension.')

    def require_empty_cart(self):
        self.get(sxs.CART_URL)
        if self.call('cart')['rows']:
            raise RuntimeError('S&S cart already contains stock. Existing items were retained; review before Reorder.')

    def open_product(self, product):
        self.get(sxs.BASE_URL + '/ps/' + quote(sxs.style_id(product), safe=''))
        selected = sxs.select_product(product, self.call('candidates')['candidates'])
        self.get(selected['url'])
        self.call('color', color=product['color'])
        return selected

    def inventory(self):
        return self.call('inventory')['inventory']

    def cart_rows(self):
        return self.call('cart')['rows']

    def verified_cart_rows(self, lines):
        expected = [{'selected': {'url': x['selected']['url']},
                     'product': {'color': x['product']['color']},
                     'quantities': x['quantities'], 'warehouse': x['warehouse']} for x in lines]
        return self.call('verified_cart', lines=expected)['rows']

    def fill_product(self, lines):
        self.call('fill', allocations=[{'warehouse': x['warehouse'], 'quantities': x['quantities']} for x in lines])
        self.call('add', quantity=sum(sum(x['quantities'].values()) for x in lines))
        self.get(sxs.CART_URL)
        if not self.cart_rows():
            raise RuntimeError('S&S cart did not confirm the stock addition; review before retrying.')

    def review_checkout(self, po):
        result = self.call('review', po=str(po))
        self._validate_review(result, po)
        return {**result, 'eta': max(sxs.parse_eta(x) for x in result['methods'])}

    @staticmethod
    def _validate_review(result, po):
        if (not re.fullmatch(r'\d{7}', str(po)) or result.get('po') != str(po)
                or result.get('payment') != 'Net 60'
                or not re.search(r'2727\s+Commerce\s+Way', result.get('address', ''), re.I)
                or not re.search(r'Philadelphia,?\s+PA\s+19154', result.get('address', ''), re.I)
                or not result.get('methods')
                or any(not re.search(r'\bUPS Ground\b', x, re.I) for x in result['methods'])):
            raise RuntimeError('S&S checkout PO, Commerce Way address, UPS Ground, or Net 60 did not verify.')

    def checkout_still_valid(self, po):
        try:
            result = self.call('valid', po=str(po))
            self._validate_review(result, po)
            return True
        except RuntimeError:
            return False

    def checkout(self, po, lines):
        sxs.validate_cart(self.verified_cart_rows(lines), lines)
        self.call('checkout')
        result = self.review_checkout(po)
        self.owned_lines = lines
        return result

    def empty_owned_cart(self):
        if not self.owned_lines:
            raise RuntimeError('No verified S&S cart belongs to this Reorder.')
        self.get(sxs.CART_URL)
        sxs.validate_cart(self.verified_cart_rows(self.owned_lines), self.owned_lines)
        rows = self.cart_rows()
        while rows:
            result = self.call('remove', rows=rows)
            following = result['rows']
            if following != rows[1:]:
                raise RuntimeError('S&S cart removal was not confirmed; review the cart.')
            rows = following
        self.owned_lines = None

    def submit(self, po):
        if str(po) != self.order_id:
            raise RuntimeError('S&S purchase PO changed.')
        self.review_checkout(po)
        self.call('begin_submit', po=str(po))
        # A lost response from the final click can only be recovered by reading
        # confirmation. Neither the transport nor this adapter repeats the click.
        try:
            self.call('confirm_submit', po=str(po))
        except SxsBridgeError:
            pass
        deadline = time.monotonic() + 40
        while True:
            state = self.call('confirmation')
            result = sxs.confirmation_from_text(state['text'], po, state['url'])
            if result:
                return result
            if time.monotonic() >= deadline:
                raise RuntimeError('S&S purchase confirmation is uncertain. Review supplier history and the saved Reorder receipt before retrying.')
            time.sleep(0.5)

    def close(self):
        self.session.close()
