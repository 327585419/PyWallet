import os

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.logger import Logger
from kivy.properties import DictProperty, ObjectProperty
from kivy.uix.floatlayout import FloatLayout
from kivy.utils import platform
from kivymd.bottomsheet import MDListBottomSheet
from requests.exceptions import ConnectionError

from pywalib import PyWalib, UnknownEtherscanException
from pywallet.about import AboutScreen
from pywallet.aliasform import AliasForm
from pywallet.flashqrcode import FlashQrCodeScreen
from pywallet.managekeystore import ManageKeystoreScreen
from pywallet.overview import OverviewScreen
from pywallet.settings import Settings
from pywallet.settings_screen import SettingsScreen
from pywallet.store import Store
from pywallet.switchaccount import SwitchAccountScreen
from pywallet.utils import (Dialog, check_request_write_permission,
                            check_write_permission, load_kv_from_py,
                            run_in_thread)

# Time before loading the next screen.
# The idea is to let the application render before trying to add child widget,
# refs #122.
SCREEN_SWITCH_DELAY = 0.4

load_kv_from_py(__file__)


class Controller(FloatLayout):

    # allownone, e.g. when the keystore is void
    current_account = ObjectProperty(allownone=True)
    # pseudo Etherscan cache, keeps a local copy of accounts balance & history
    # accounts_balance[account_0xaddress]
    accounts_balance = DictProperty({})
    # accounts_history[account_0xaddress]
    accounts_history = DictProperty({})

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pywalib = None
        self.screen_history = []
        self.register_event_type('on_alias_updated')
        Clock.schedule_once(lambda dt: self.load_landing_page())
        Window.bind(on_keyboard=self.on_keyboard)

    def on_keyboard(self, window, key, *args):
        """
        Handles the back button (Android) and ESC key.
        Goes back to the previous screen or quite the application
        if there's none left.
        """
        if key == 27:
            screen_manager = self.screen_manager
            # if we already are in the overview screen, also move back to
            # the overview subtab of the overview screen
            if screen_manager.current == 'overview':
                overview_bnavigation = self.overview_bnavigation
                tab_manager = overview_bnavigation.ids['tab_manager']
                if tab_manager.current != 'overview':
                    self.select_overview_subtab()
                    return True
                else:
                    # if we were already in the overview:overview subtab,
                    # then propagates the key which in this case will exit
                    # the application
                    return False
            self.screen_manager_previous()
            # stops the propagation
            return True
        return False

    @property
    def overview_bnavigation(self):
        screen_manager = self.screen_manager
        overview_screen = screen_manager.get_screen('overview')
        overview_bnavigation = overview_screen.ids.overview_bnavigation_id
        return overview_bnavigation

    @property
    def overview(self):
        overview_bnavigation = self.overview_bnavigation
        return overview_bnavigation.ids.overview_id

    @property
    def history(self):
        return self.overview.ids.history_id

    @property
    def switch_account(self):
        screen_manager = self.screen_manager
        switch_account_screen = screen_manager.get_screen('switch_account')
        switch_account_id = switch_account_screen.ids.switch_account_id
        return switch_account_id

    @property
    def send(self):
        overview_bnavigation = self.overview_bnavigation
        return overview_bnavigation.ids.send_id

    @property
    def manage_keystores(self):
        screen_manager = self.screen_manager
        manage_keystores_screen = screen_manager.get_screen('manage_keystores')
        manage_keystores_bnavigation_id = \
            manage_keystores_screen.ids.manage_keystores_id
        return manage_keystores_bnavigation_id

    @property
    def about(self):
        screen_manager = self.screen_manager
        about_screen = screen_manager.get_screen('about')
        about_id = about_screen.ids.about_id
        return about_id

    @property
    def manage_existing(self):
        manage_keystores = self.manage_keystores
        return manage_keystores.ids.manage_existing_id

    @property
    def create_new_account(self):
        manage_keystores = self.manage_keystores
        return manage_keystores.ids.create_new_account_id

    @property
    def toolbar(self):
        return self.ids.toolbar_id

    @property
    def screen_manager(self):
        return self.ids.screen_manager_id

    @property
    def pywalib(self):
        """
        Gets or creates the PyWalib object.
        Also recreates the object if the keystore_path changed.
        """
        keystore_path = Settings.get_keystore_path()
        chain_id = Settings.get_stored_network()
        if self._pywalib is None or \
                self._pywalib.keystore_dir != keystore_path or \
                self._pywalib.chain_id != chain_id:
            self._pywalib = PyWalib(
                keystore_dir=keystore_path, chain_id=chain_id)
        return self._pywalib

    def set_toolbar_title(self, title):
        self.toolbar.title_property = title

    def bind_current_account_balance(self):
        """
        Binds the accounts_balance to the Toolbar title.
        """
        self.bind(accounts_balance=self.update_toolbar_title_balance)

    def unbind_current_account_balance(self):
        """
        Unbinds the accounts_balance from the Toolbar title.
        """
        self.unbind(accounts_balance=self.update_toolbar_title_balance)

    def screen_manager_current(self, current, direction=None, history=True):
        screens = {
            'overview': OverviewScreen,
            'switch_account': SwitchAccountScreen,
            'manage_keystores': ManageKeystoreScreen,
            'flashqrcode': FlashQrCodeScreen,
            'settings_screen': SettingsScreen,
            'about': AboutScreen,
        }
        screen_manager = self.screen_manager
        # creates the Screen object if it doesn't exist
        if not screen_manager.has_screen(current):
            screen = screens[current](name=current)
            screen_manager.add_widget(screen)
        if direction is not None:
            screen_manager.transition.direction = direction
        screen_manager.current = current
        if history:
            # do not update history if it's the same screen because we do not
            # want the go back button to behave like it was doing nothing
            if not self.screen_history or self.screen_history[-1] != current:
                self.screen_history.append(current)
        # in this case let's reset since the overview is the root screen
        # because we never want the back button to bring us from overview
        # to another screen
        if current == 'overview':
            self.screen_history = []

    def screen_manager_previous(self):
        try:
            previous_screen = self.screen_history.pop(-2)
        except IndexError:
            previous_screen = 'overview'
        self.screen_manager_current(
            previous_screen, direction='right', history=False)

    @staticmethod
    def patch_keystore_path():
        """
        Changes pywalib default keystore path depending on platform.
        Currently only updates it on Android.
        """
        if platform != "android":
            return
        import pywalib
        # uses kivy user_data_dir (/sdcard/<app_name>)
        pywalib.KEYSTORE_DIR_PREFIX = App.get_running_app().user_data_dir

    @classmethod
    def delete_account_alias(cls, account):
        """
        Deletes the alias for the given account.
        """
        address = "0x" + account.address.hex()
        store = Store.get_store()
        alias_dict = store['alias']
        alias_dict.pop(address)
        store['alias'] = alias_dict

    @classmethod
    def set_account_alias(cls, account, alias):
        """
        Sets an alias for a given Account object.
        Deletes the alias if empty.
        """
        # if the alias is empty and an alias exists for this address,
        # deletes it
        if alias == '':
            try:
                cls.delete_account_alias(account)
            except KeyError:
                pass
            return
        address = "0x" + account.address.hex()
        store = Store.get_store()
        try:
            alias_dict = store['alias']
        except KeyError:
            # creates store if doesn't yet exists
            store.put('alias')
            alias_dict = store['alias']
        alias_dict.update({address: alias})
        store['alias'] = alias_dict

    @classmethod
    def get_address_alias(cls, address):
        """
        Returns the alias of the given address string.
        """
        store = Store.get_store()
        return store.get('alias')[address]

    @classmethod
    def get_account_alias(cls, account):
        """
        Returns the alias of the given Account object.
        """
        address = "0x" + account.address.hex()
        return cls.get_address_alias(address)

    @staticmethod
    def src_dir():
        return os.path.join(
                os.path.dirname(os.path.abspath(__file__)), '..')

    @mainthread
    def update_toolbar_title_balance(self, instance=None, value=None):
        if self.current_account is None:
            return
        address = '0x' + self.current_account.address.hex()
        try:
            balance = self.accounts_balance[address]
        except KeyError:
            balance = 0
        title = "%s ETH" % (balance)
        self.set_toolbar_title(title)

    def show_storage_permissions_required_dialog(self):
        title = "External storage permissions required"
        body = ""
        body += "In order to save your keystore, PyWallet requires access "
        body += "to your device storage. "
        body += "Please allow PyWallet to access it when prompted."
        dialog = Dialog.create_dialog(title, body)
        dialog.open()
        return dialog

    def check_external_storage_permission(self, callback):
        """
        Checks for external storage permissions and pops a dialog to ask for it
        if needed.
        Returns True if the permission was already granted, otherwise prompts
        for permissions dialog (async) and returns False.
        """
        if check_write_permission():
            return True
        dialog = self.show_storage_permissions_required_dialog()
        dialog.bind(
            on_dismiss=lambda *x: check_request_write_permission(
                callback))
        return False

    def try_load_current_account(self):
        """
        Load the main account or fallback to the create account screen.
        """
        try:
            # will trigger account data fetching
            self.current_account = self.pywalib.get_main_account()
            if SCREEN_SWITCH_DELAY:
                Clock.schedule_once(
                    lambda dt: self.screen_manager_current('overview'),
                    SCREEN_SWITCH_DELAY)
            else:
                self.screen_manager_current('overview')
        except IndexError:
            self.load_create_new_account()

    def load_landing_page(self):
        """
        Loads the landing page.
        """
        @mainthread
        def on_permissions_callback(permissions, grant_results):
            """
            On write permission callback, toggles loading account from
            persistent keystore if granted.
            Also loads the current account to the app.
            This is called from the Java thread, hence the `@mainthread`.
            Find out more on the p4a permissions callback in:
            https://github.com/kivy/python-for-android/pull/1818
            """
            if all(grant_results):
                Settings.set_is_persistent_keystore(True)
            self.try_load_current_account()
        # if no permission yet, the try_load_current_account() call will be
        # async from the callback
        if self.check_external_storage_permission(
                callback=on_permissions_callback):
            self.try_load_current_account()

    @run_in_thread
    def fetch_balance(self):
        """
        Fetches the new balance & sets accounts_balance property.
        """
        if self.current_account is None:
            return
        address = '0x' + self.current_account.address.hex()
        chain_id = Settings.get_stored_network()
        try:
            balance = PyWalib.get_balance(address, chain_id)
        except ConnectionError:
            Dialog.on_balance_connection_error()
            Logger.warning('ConnectionError', exc_info=True)
            return
        except ValueError:
            # most likely the JSON object could not be decoded, refs #91
            # currently logged as an error, because we want more insight
            # in order to eventually handle it more specifically
            Dialog.on_balance_value_error()
            Logger.error('ValueError', exc_info=True)
            return
        except UnknownEtherscanException:
            # also handles uknown errors, refs #112
            Dialog.on_balance_unknown_error()
            Logger.error('UnknownEtherscanException', exc_info=True)
            return
        # triggers accounts_balance observers update
        self.accounts_balance[address] = balance

    def on_update_alias_clicked(self, dialog, alias):
        account = self.current_account
        Controller.set_account_alias(account, alias)
        # makes sure widgets that already rendered the address get updated
        self.dispatch('on_alias_updated', alias)
        dialog.dismiss()

    def on_alias_updated(self, *args):
        pass

    def copy_address_clipboard(self):
        """
        Copies the current account address to the clipboard.
        """
        account = self.current_account
        address = "0x" + account.address.hex()
        Clipboard.copy(address)

    def prompt_alias_dialog(self):
        account = self.current_account
        dialog = AliasForm.create_alias_dialog(account)
        dialog.add_action_button(
            "Update",
            action=lambda *x: self.on_update_alias_clicked(
                dialog, dialog.content.alias))
        dialog.open()

    def open_address_options(self):
        """
        Loads the address options bottom sheet.
        """
        bottom_sheet = MDListBottomSheet()
        bottom_sheet.add_item(
            'Switch account',
            lambda x: self.load_switch_account(), icon='swap-horizontal')
        bottom_sheet.add_item(
            'Change alias',
            lambda x: self.prompt_alias_dialog(), icon='information')
        bottom_sheet.add_item(
            'Copy address',
            lambda x: self.copy_address_clipboard(), icon='content-copy')
        bottom_sheet.open()

    def select_overview_subtab(self):
        """
        Selects the overview sub tab.
        """
        # this is what we would normally do:
        # tab_manager.current = 'overview'
        # but instead we need to simulate the click on the
        # navigation bar children or the associated screen button
        # would not have the selected color
        overview_bnavigation = self.overview_bnavigation
        navigation_bar = overview_bnavigation.children[0]
        boxlayout = navigation_bar.children[0]
        nav_headers = boxlayout.children
        # the overview is the first/last button
        overview_nav_header = nav_headers[-1]
        overview_nav_header.dispatch('on_press')

    def load_switch_account(self):
        """
        Loads the switch account screen.
        """
        # loads the switch account screen
        Clock.schedule_once(
            lambda dt: self.screen_manager_current(
                'switch_account', direction='left'),
            SCREEN_SWITCH_DELAY)

    def load_manage_keystores(self):
        """
        Loads the manage keystores screen.
        """
        # loads the manage keystores screen
        if SCREEN_SWITCH_DELAY:
            Clock.schedule_once(
                lambda dt: self.screen_manager_current(
                    'manage_keystores', direction='left'),
                SCREEN_SWITCH_DELAY)
        else:
            self.screen_manager_current(
                'manage_keystores', direction='left')

    def load_create_new_account(self):
        """
        Loads the create new account tab from the manage keystores screen.
        """
        # we need the screen now
        global SCREEN_SWITCH_DELAY
        saved_delay = SCREEN_SWITCH_DELAY
        SCREEN_SWITCH_DELAY = None
        self.load_manage_keystores()
        SCREEN_SWITCH_DELAY = saved_delay
        # loads the create new account tab
        manage_keystores = self.manage_keystores
        create_new_account_nav_item = \
            manage_keystores.ids.create_new_account_nav_item_id
        create_new_account_nav_item.dispatch('on_tab_press')

    def load_flash_qr_code(self):
        """
        Loads the flash QR Code screen.
        """
        # loads ZBarCam only when needed, refs:
        # https://github.com/AndreMiras/PyWallet/issues/94
        from kivy_garden.zbarcam import ZBarCam  # noqa
        # loads the flash QR Code screen
        self.screen_manager_current('flashqrcode', direction='left')

    def load_settings_screen(self):
        """
        Loads the settings screen.
        """
        if SCREEN_SWITCH_DELAY:
            Clock.schedule_once(
                lambda dt: self.screen_manager_current(
                    'settings_screen', direction='left'),
                SCREEN_SWITCH_DELAY)
        else:
            self.screen_manager_current(
                'settings_screen', direction='left')

    def load_about_screen(self):
        """
        Loads the about screen.
        """
        Clock.schedule_once(
            lambda dt: self.screen_manager_current('about', direction='left'),
            SCREEN_SWITCH_DELAY)
