import os
import sys
import threading
import time

from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, QTimer, QRect, pyqtSignal
from PyQt5.QtGui import QPen, QPainter, QPalette
from PyQt5.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget,
                             QFileDialog, QSlider, QGridLayout, QDialog, QApplication)

from electrum.bip32 import is_bip32_derivation, BIP32Node, normalize_bip32_derivation, xpub_type
from electrum.daemon import Daemon
from electrum.i18n import _
from electrum.keystore import bip44_derivation, bip39_to_seed, purpose48_derivation, ScriptTypeNotSupported
from electrum.plugin import run_hook, HardwarePluginLibraryUnavailable
from electrum.storage import StorageReadWriteError
from electrum.util import WalletFileException, get_new_wallet_name, UserCancelled, UserFacingException
from electrum.wallet import wallet_types
from .wizard import QEAbstractWizard, WizardComponent
from electrum.logging import get_logger, Logger
from electrum import WalletStorage, mnemonic, keystore
from electrum.wizard import NewWalletWizard

from electrum.gui.qt.bip39_recovery_dialog import Bip39RecoveryDialog
from electrum.gui.qt.password_dialog import PasswordLayout, PW_NEW, MSG_ENTER_PASSWORD, PasswordLayoutForHW
from electrum.gui.qt.seed_dialog import SeedLayout, MSG_PASSPHRASE_WARN_ISSUE4566, KeysLayout
from electrum.gui.qt.util import (PasswordLineEdit, char_width_in_lineedit, WWLabel, InfoButton, font_height,
                                  ChoiceWidget, MessageBoxMixin)

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig
    from electrum.plugin import Plugins
    from electrum.daemon import Daemon
    from electrum.wallet_db import WalletDB
    from electrum.gui.qt import QElectrumApplication

WIF_HELP_TEXT = (_('WIF keys are typed in Electrum, based on script type.') + '\n\n' +
                 _('A few examples') + ':\n' +
                 'p2pkh:KxZcY47uGp9a...       \t-> 1DckmggQM...\n' +
                 'p2wpkh-p2sh:KxZcY47uGp9a... \t-> 3NhNeZQXF...\n' +
                 'p2wpkh:KxZcY47uGp9a...      \t-> bc1q3fjfk...')

MSG_HW_STORAGE_ENCRYPTION = _("Set wallet file encryption.") + '\n'\
                          + _("Your wallet file does not contain secrets, mostly just metadata. ") \
                          + _("It also contains your master public key that allows watching your addresses.") + '\n\n'\
                          + _("Note: If you enable this setting, you will need your hardware device to open your wallet.")


class QENewWalletWizard(NewWalletWizard, QEAbstractWizard, MessageBoxMixin):
    _logger = get_logger(__name__)

    def __init__(self, config: 'SimpleConfig', app: 'QElectrumApplication', plugins: 'Plugins', daemon: Daemon, path, *, start_viewstate=None):
        NewWalletWizard.__init__(self, daemon, plugins)
        QEAbstractWizard.__init__(self, config, app, start_viewstate=start_viewstate)

        self.setWindowTitle(_('Create/Restore wallet'))

        self._path = path

        # attach gui classes to views
        self.navmap_merge({
            'wallet_name': { 'gui': WCWalletName },
            'wallet_type': { 'gui': WCWalletType },
            'keystore_type': { 'gui': WCKeystoreType },
            'create_seed': { 'gui': WCCreateSeed },
            'confirm_seed': { 'gui': WCConfirmSeed },
            'have_seed': { 'gui': WCHaveSeed },
            'choose_hardware_device': { 'gui': WCChooseHWDevice },
            'script_and_derivation': { 'gui': WCScriptAndDerivation},
            'have_master_key': { 'gui': WCHaveMasterKey },
            'multisig': { 'gui': WCMultisig },
            'multisig_cosigner_keystore': { 'gui': WCCosignerKeystore },
            'multisig_cosigner_key': { 'gui': WCHaveMasterKey },
            'multisig_cosigner_seed': { 'gui': WCHaveSeed },
            'multisig_cosigner_hardware': { 'gui': WCChooseHWDevice },
            'multisig_cosigner_script_and_derivation': { 'gui': WCScriptAndDerivation},
            'imported': { 'gui': WCImport },
            'wallet_password': { 'gui': WCWalletPassword },
            'wallet_password_hardware': { 'gui': WCWalletPasswordHardware }
        })

        # add open existing wallet from wizard, incl hw unlock
        self.navmap_merge({
            'wallet_name': {
                'next': lambda d: 'hw_unlock' if d['wallet_needs_hw_unlock'] else 'wallet_type',
                'last': lambda d: d['wallet_exists'] and not d['wallet_needs_hw_unlock']
            },
            'hw_unlock': {
                'gui': WCChooseHWDevice,
                'next': lambda d: self.on_hardware_device(d, new_wallet=False)
            }
        })

        # insert seed extension entry/confirm as separate views
        self.navmap_merge({
            'create_seed': {
                'next': lambda d: 'create_ext' if self.wants_ext(d) else 'confirm_seed'
            },
            'create_ext': {
                'next': 'confirm_seed',
                'gui': WCEnterExt
            },
            'confirm_seed': {
                'next': lambda d: 'confirm_ext' if self.wants_ext(d) else self.on_have_or_confirm_seed(d),
                'accept': lambda d: None if self.wants_ext(d) else self.maybe_master_pubkey(d)
            },
            'confirm_ext': {
                'next': self.on_have_or_confirm_seed,
                'accept': self.maybe_master_pubkey,
                'gui': WCConfirmExt
            },
            'have_seed': {
                'next': lambda d: 'have_ext' if self.wants_ext(d) else self.on_have_or_confirm_seed(d),
                'last': lambda d: self.is_single_password() and not
                                  (self.needs_derivation_path(d) or self.is_multisig(d) or self.wants_ext(d))
            },
            'have_ext': {
                'next': self.on_have_or_confirm_seed,
                'accept': self.maybe_master_pubkey,
                'gui': WCEnterExt
            },
            'multisig_cosigner_seed': {
                'next': lambda d: 'multisig_cosigner_have_ext' if self.wants_ext(d) else self.on_have_cosigner_seed(d),
                'last': lambda d: self.is_single_password() and self.last_cosigner(d) and not
                                  (self.needs_derivation_path(d) or self.wants_ext(d))
            },
            'multisig_cosigner_have_ext': {
                'next': self.on_have_cosigner_seed,
                'last': lambda d: self.is_single_password() and self.last_cosigner(d) and not self.needs_derivation_path(d),
                'gui': WCEnterExt
            },
        })

        run_hook('init_wallet_wizard', self)


    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, path):
        self._path = path

    def is_single_password(self):
        # not supported on desktop
        return False

    def create_storage(self, single_password: str = None):
        self._logger.info('Creating wallet from wizard data')
        data = self.get_wizard_data()

        path = os.path.join(os.path.dirname(self._daemon.config.get_wallet_path()), data['wallet_name'])

        super().create_storage(path, data)

        # minimally populate self after create
        self._password = data['password']
        self.path = path

    def run_upgrades(self, db: 'WalletDB') -> None:
        storage = db.storage
        path = storage.path
        if db.requires_split():
            msg = _(
                "The wallet '{}' contains multiple accounts, which are no longer supported since Electrum 2.7.\n\n"
                "Do you want to split your wallet into multiple files?").format(path)
            if not self.question(msg):
                return
            file_list = db.split_accounts(path)
            msg = _('Your accounts have been moved to') + ':\n' + '\n'.join(file_list) + '\n\n' + _(
                'Do you want to delete the old file') + ':\n' + path
            if self.question(msg):
                os.remove(path)
                self.show_warning(_('The file was removed'))
            # raise now, to avoid having the old storage opened
            raise UserCancelled()
        if db.requires_upgrade():
            self.waiting_dialog(db.upgrade, _('Upgrading wallet format...'))

    def waiting_dialog(self, task, msg, on_finished=None):
        dialog = QDialog()
        label = WWLabel(msg)
        vbox = QVBoxLayout()
        vbox.addSpacing(100)
        label.setMinimumWidth(300)
        label.setAlignment(Qt.AlignCenter)
        vbox.addWidget(label)
        vbox.addSpacing(100)
        dialog.setLayout(vbox)
        dialog.setModal(True)

        exc = None

        def task_wrap(task):
            nonlocal exc
            try:
                task()
            except Exception as e:
                exc = e

        t = threading.Thread(target=task_wrap, args=(task,))
        t.start()

        dialog.show()

        while True:
            QApplication.processEvents()
            t.join(1.0/60)
            if not t.is_alive():
                break

        dialog.close()

        if exc:
            raise exc

        if on_finished:
            on_finished()


class WCWalletName(WizardComponent, Logger):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Electrum wallet'))
        Logger.__init__(self)

        path = wizard._path

        if os.path.isdir(path):
            raise Exception("wallet path cannot point to a directory")

        self.wallet_exists = False
        self.wallet_is_open = False
        self.wallet_needs_hw_unlock = False

        hbox = QHBoxLayout()
        hbox.addWidget(QLabel(_('Wallet') + ':'))
        self.name_e = QLineEdit()
        hbox.addWidget(self.name_e)
        button = QPushButton(_('Choose...'))
        hbox.addWidget(button)
        self.layout().addLayout(hbox)

        msg_label = WWLabel('')
        self.layout().addWidget(msg_label)
        hbox2 = QHBoxLayout()
        self.pw_e = PasswordLineEdit('', self)
        self.pw_e.setFixedWidth(17 * char_width_in_lineedit())
        pw_label = QLabel(_('Password') + ':')
        hbox2.addWidget(pw_label)
        hbox2.addWidget(self.pw_e)
        hbox2.addStretch()
        self.layout().addLayout(hbox2)

        self.layout().addSpacing(50)
        vbox_create_new = QVBoxLayout()
        vbox_create_new.addWidget(QLabel(_('Alternatively') + ':'), alignment=Qt.AlignLeft)
        button_create_new = QPushButton(_('Create New Wallet'))
        button_create_new.setMinimumWidth(120)
        vbox_create_new.addWidget(button_create_new, alignment=Qt.AlignLeft)
        widget_create_new = QWidget()
        widget_create_new.setLayout(vbox_create_new)
        vbox_create_new.setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(widget_create_new)
        self.layout().addStretch(1)

        temp_storage = None  # type: Optional[WalletStorage]
        wallet_folder = os.path.dirname(path)

        def on_choose():
            path, __ = QFileDialog.getOpenFileName(self, "Select your wallet file", wallet_folder)
            if path:
                self.name_e.setText(path)

        def on_filename(filename):
            # FIXME? "filename" might contain ".." (etc) and hence sketchy path traversals are possible
            nonlocal temp_storage
            temp_storage = None
            msg = None
            self.wallet_exists = False
            self.wallet_is_open = False
            self.wallet_needs_hw_unlock = False
            if filename:
                path = os.path.join(wallet_folder, filename)
                wallet_from_memory = self.wizard._daemon.get_wallet(path)
                try:
                    if wallet_from_memory:
                        temp_storage = wallet_from_memory.storage  # type: Optional[WalletStorage]
                        self.wallet_is_open = True
                    else:
                        temp_storage = WalletStorage(path)
                    self.wallet_exists = temp_storage.file_exists()
                except (StorageReadWriteError, WalletFileException) as e:
                    msg = _('Cannot read file') + f'\n{repr(e)}'
                except Exception as e:
                    self.logger.exception('')
                    msg = _('Cannot read file') + f'\n{repr(e)}'
            else:
                msg = ""
            self.valid = temp_storage is not None
            user_needs_to_enter_password = False
            if temp_storage:
                if not temp_storage.file_exists():
                    msg = _("This file does not exist.") + '\n' \
                          + _("Press 'Next' to create this wallet, or choose another file.")
                elif not wallet_from_memory:
                    if temp_storage.is_encrypted_with_user_pw():
                        msg = _("This file is encrypted with a password.") + '\n' \
                              + _('Enter your password or choose another file.')
                        user_needs_to_enter_password = True
                    elif temp_storage.is_encrypted_with_hw_device():
                        msg = _("This file is encrypted using a hardware device.") + '\n' \
                              + _("Press 'Next' to choose device to decrypt.")
                        self.wallet_needs_hw_unlock = True
                    else:
                        msg = _("Press 'Finish' to open this wallet.")
                else:
                    msg = _("This file is already open in memory.") + "\n" \
                          + _("Press 'Finish' to create/focus window.")
            if msg is None:
                msg = _('Cannot read file')
            msg_label.setText(msg)
            widget_create_new.setVisible(bool(temp_storage and temp_storage.file_exists()))
            if user_needs_to_enter_password:
                pw_label.show()
                self.pw_e.show()
            else:
                pw_label.hide()
                self.pw_e.hide()
            self.on_updated()

        button.clicked.connect(on_choose)
        button_create_new.clicked.connect(
            lambda: self.name_e.setText(get_new_wallet_name(wallet_folder)))  # FIXME get_new_wallet_name might raise
        self.name_e.textChanged.connect(on_filename)
        self.name_e.setText(os.path.basename(path))

    def apply(self):
        if self.wallet_exists:
            # use full path
            path = self.wizard._path
            wallet_folder = os.path.dirname(path)
            self.wizard_data['wallet_name'] = os.path.join(wallet_folder, self.name_e.text())
        else:
            self.wizard_data['wallet_name'] = self.name_e.text()
        self.wizard_data['wallet_exists'] = self.wallet_exists
        self.wizard_data['wallet_is_open'] = self.wallet_is_open
        self.wizard_data['password'] = self.pw_e.text()
        self.wizard_data['wallet_needs_hw_unlock'] = self.wallet_needs_hw_unlock


class WCWalletType(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Create new wallet'))
        message = _('What kind of wallet do you want to create?')
        wallet_kinds = [
            ('standard',  _('Standard wallet')),
            ('2fa',       _('Wallet with two-factor authentication')),
            ('multisig',  _('Multi-signature wallet')),
            ('imported',  _('Import Bitcoin addresses or private keys')),
        ]
        choices = [pair for pair in wallet_kinds if pair[0] in wallet_types]

        self.choice_w = ChoiceWidget(message=message, choices=choices, selected='standard')
        self.layout().addWidget(self.choice_w)
        self.layout().addStretch(1)
        self._valid = True

    def apply(self):
        self.wizard_data['wallet_type'] = self.choice_w.selected_item[0]


class WCKeystoreType(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Keystore'))
        message = _('Do you want to create a new seed, or to restore a wallet using an existing seed?')
        choices = [
            ('createseed', _('Create a new seed')),
            ('haveseed',   _('I already have a seed')),
            ('masterkey',  _('Use a master key')),
            ('hardware',   _('Use a hardware device'))
        ]

        self.choice_w = ChoiceWidget(message=message, choices=choices)
        self.layout().addWidget(self.choice_w)
        self.layout().addStretch(1)
        self._valid = True

    def apply(self):
        self.wizard_data['keystore_type'] = self.choice_w.selected_item[0]


class WCCreateSeed(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Wallet Seed'))
        self._busy = True
        self.seed_type = 'standard' if self.wizard.config.WIZARD_DONT_CREATE_SEGWIT else 'segwit'
        self.slayout = None
        self.seed = None

    def on_ready(self):
        if self.wizard_data['wallet_type'] == '2fa':
            self.seed_type = '2fa_segwit'
        QTimer.singleShot(1, self.create_seed)

    def apply(self):
        if self.slayout:
            self.wizard_data['seed'] = self.seed
            self.wizard_data['seed_type'] = self.seed_type
            self.wizard_data['seed_extend'] = self.slayout.is_ext
            self.wizard_data['seed_variant'] = 'electrum'
            self.wizard_data['seed_extra_words'] = ''  # empty default

    def create_seed(self):
        self.busy = True
        self.seed = mnemonic.Mnemonic('en').make_seed(seed_type=self.seed_type)

        self.slayout = SeedLayout(
            title=_('Your wallet generation seed is:'),
            seed=self.seed,
            options=['ext'],
            msg=True,
            parent=self,
            config=self.wizard.config,
        )
        self.layout().addLayout(self.slayout)
        self.layout().addStretch(1)
        self.busy = False
        self.valid = True


class WCConfirmSeed(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Confirm Seed'))
        message = ' '.join([
            _('Your seed is important!'),
            _('If you lose your seed, your money will be permanently lost.'),
            _('To make sure that you have properly saved your seed, please retype it here.')
        ])

        self.layout().addWidget(WWLabel(message))

        # TODO: SeedLayout assumes too much in parent, refactor SeedLayout
        # for now, fake parent.next_button.setEnabled
        class Hack:
            def setEnabled(self2, b):
                self.valid = b
        self.next_button = Hack()

        self.slayout = SeedLayout(
            is_seed=lambda x: x == self.wizard_data['seed'],
            parent=self,
            config=self.wizard.config,
        )
        self.layout().addLayout(self.slayout)

    def apply(self):
        pass


class WCEnterExt(WizardComponent, Logger):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Seed Extension'))
        Logger.__init__(self)

        message = '\n'.join([
            _('You may extend your seed with custom words.'),
            _('Your seed extension must be saved together with your seed.'),
        ])
        warning = '\n'.join([
            _('Note that this is NOT your encryption password.'),
            _('If you do not know what this is, leave this field empty.'),
        ])

        self.ext_edit = SeedExtensionEdit(self, message=message, warning=warning)
        self.ext_edit.textEdited.connect(self.on_text_edited)
        self.layout().addWidget(self.ext_edit)
        self.layout().addStretch(1)

    def on_text_edited(self, text):
        # TODO also for cosigners?
        self.ext_edit.warn_issue4566 = self.wizard_data['keystore_type'] == 'haveseed' and \
                                       self.wizard_data['seed_type'] == 'bip39'
        self.validate()

    def validate(self):
        self.apply()
        text = self.ext_edit.text()
        if len(text) == 0:
            self.valid = False
            return

        cosigner_data = self.wizard.current_cosigner(self.wizard_data)

        if self.wizard_data['wallet_type'] == 'multisig':
            if 'seed_variant' in cosigner_data and cosigner_data['seed_variant'] in ['bip39', 'slip39']:
                # defer validation to when derivation path is known
                self.valid = True
            else:
                if self.wizard.has_duplicate_masterkeys(self.wizard_data):
                    self.logger.debug('Duplicate master keys!')
                    # TODO: user feedback
                    self.valid = False
                elif self.wizard.has_heterogeneous_masterkeys(self.wizard_data):
                    self.logger.debug('Heterogenous master keys!')
                    # TODO: user feedback
                    self.valid = False
                else:
                    self.valid = True
        else:
            self.valid = True

    def apply(self):
        cosigner_data = self.wizard.current_cosigner(self.wizard_data)
        cosigner_data['seed_extra_words'] = self.ext_edit.text()


class WCConfirmExt(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Confirm Seed Extension'))
        message = '\n'.join([
            _('Your seed extension must be saved together with your seed.'),
            _('Please type it here.'),
        ])
        self.ext_edit = SeedExtensionEdit(self, message=message)
        self.ext_edit.textEdited.connect(self.on_text_edited)
        self.layout().addWidget(self.ext_edit)
        self.layout().addStretch(1)

    def on_text_edited(self, text):
        self.valid = text == self.wizard_data['seed_extra_words']

    def apply(self):
        pass


class WCHaveSeed(WizardComponent, Logger):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Enter Seed'))
        Logger.__init__(self)

        self.layout().addWidget(WWLabel(_('Please enter your seed phrase in order to restore your wallet.')))

        # TODO: SeedLayout assumes too much in parent, refactor SeedLayout
        # for now, fake parent.next_button.setEnabled
        class Hack:
            def setEnabled(self2, b):
                if not b:
                    self.valid = b
                else:
                    self.validate()

        self.next_button = Hack()

    def on_ready(self):
        options = ['ext'] if self.wizard_data['wallet_type'] == '2fa' else ['ext', 'bip39', 'slip39']
        self.slayout = SeedLayout(
            is_seed=self.is_seed,
            options=options,
            parent=self,
            config=self.wizard.config,
        )
        self.slayout.updated.connect(self.validate)

        self.layout().addLayout(self.slayout)
        self.layout().addStretch(1)

    def is_seed(self, x):
        if self.wizard_data['wallet_type'] == 'standard':
            return mnemonic.is_seed(x)
        elif self.wizard_data['wallet_type'] == '2fa':
            return mnemonic.seed_type(x) in ['2fa', '2fa_segwit']
        else:
            return mnemonic.seed_type(x) in ['standard', 'segwit']

    def validate(self):
        # precond: only call when SeedLayout deems seed a valid seed
        seed = self.slayout.get_seed()
        seed_variant = self.slayout.seed_type
        wallet_type = self.wizard_data['wallet_type']
        seed_valid, seed_type, validation_message = self.wizard.validate_seed(seed, seed_variant, wallet_type)

        is_cosigner = self.wizard_data['wallet_type'] == 'multisig' and 'multisig_current_cosigner' in self.wizard_data

        if not is_cosigner or not seed_valid:
            self.valid = seed_valid
            return

        if seed_type in ['bip39', 'slip39'] or self.slayout.is_ext:
            # defer validation to when derivation path and/or passphrase/ext is known
            self.valid = True
        else:
            self.apply()
            if self.wizard.has_duplicate_masterkeys(self.wizard_data):
                self.logger.debug('Duplicate master keys!')
                # TODO: user feedback
                seed_valid = False
            elif self.wizard.has_heterogeneous_masterkeys(self.wizard_data):
                self.logger.debug('Heterogenous master keys!')
                # TODO: user feedback
                seed_valid = False

            self.valid = seed_valid

    def apply(self):
        cosigner_data = self.wizard.current_cosigner(self.wizard_data)

        cosigner_data['seed'] = self.slayout.get_seed()
        cosigner_data['seed_variant'] = self.slayout.seed_type
        if self.slayout.seed_type == 'electrum':
            cosigner_data['seed_type'] = mnemonic.seed_type(self.slayout.get_seed())
        else:
            cosigner_data['seed_type'] = self.slayout.seed_type
        cosigner_data['seed_extend'] = self.slayout.is_ext
        cosigner_data['seed_extra_words'] = ''  # empty default


class WCScriptAndDerivation(WizardComponent, Logger):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Script type and Derivation path'))
        Logger.__init__(self)

    def on_ready(self):
        message1 = _('Choose the type of addresses in your wallet.')
        message2 = ' '.join([
            _('You can override the suggested derivation path.'),
            _('If you are not sure what this is, leave this field unchanged.')
        ])
        hide_choices = False

        if self.wizard_data['wallet_type'] == 'multisig':
            choices = [
                # TODO: nicer to refactor 'standard' to 'p2sh', but backend wallet still uses 'standard'
                ('standard', 'legacy multisig (p2sh)', normalize_bip32_derivation("m/45'/0")),
                ('p2wsh-p2sh', 'p2sh-segwit multisig (p2wsh-p2sh)', purpose48_derivation(0, xtype='p2wsh-p2sh')),
                ('p2wsh', 'native segwit multisig (p2wsh)', purpose48_derivation(0, xtype='p2wsh')),
            ]
            if 'multisig_current_cosigner' in self.wizard_data:
                # get script type of first cosigner
                ks = self.wizard.keystore_from_data(self.wizard_data['wallet_type'], self.wizard_data)
                default_choice = xpub_type(ks.get_master_public_key())
                hide_choices = True
            else:
                default_choice = 'p2wsh'
        else:
            default_choice = 'p2wpkh'
            choices = [
                # TODO: nicer to refactor 'standard' to 'p2pkh', but backend wallet still uses 'standard'
                ('standard', 'legacy (p2pkh)', bip44_derivation(0, bip43_purpose=44)),
                ('p2wpkh-p2sh', 'p2sh-segwit (p2wpkh-p2sh)', bip44_derivation(0, bip43_purpose=49)),
                ('p2wpkh', 'native segwit (p2wpkh)', bip44_derivation(0, bip43_purpose=84)),
            ]

        if self.wizard_data['wallet_type'] == 'standard' and not self.wizard_data['keystore_type'] == 'hardware':
            button = QPushButton(_("Detect Existing Accounts"))

            passphrase = self.wizard_data['seed_extra_words'] if self.wizard_data['seed_extend'] else ''
            if self.wizard_data['seed_variant'] == 'bip39':
                root_seed = bip39_to_seed(self.wizard_data['seed'], passphrase)
            elif self.wizard_data['seed_variant'] == 'slip39':
                root_seed = self.wizard_data['seed'].decrypt(passphrase)

            def get_account_xpub(account_path):
                root_node = BIP32Node.from_rootseed(root_seed, xtype="standard")
                account_node = root_node.subkey_at_private_derivation(account_path)
                account_xpub = account_node.to_xpub()
                return account_xpub

            def on_account_select(account):
                script_type = account["script_type"]
                if script_type == "p2pkh":
                    script_type = "standard"
                button_index = self.c_values.index(script_type)
                button = self.clayout.group.buttons()[button_index]
                button.setChecked(True)
                self.derivation_path_edit.setText(account["derivation_path"])

            button.clicked.connect(lambda: Bip39RecoveryDialog(self, get_account_xpub, on_account_select))
            self.layout().addWidget(button, alignment=Qt.AlignLeft)
            self.layout().addWidget(QLabel(_("Or")))

        def on_choice_click(index):
            self.derivation_path_edit.setText(self.choice_w.selected_item[2])
        self.choice_w = ChoiceWidget(message=message1, choices=choices, selected=default_choice)
        self.choice_w.itemSelected.connect(on_choice_click)

        if not hide_choices:
            self.layout().addWidget(self.choice_w)

        self.layout().addWidget(WWLabel(message2))

        self.derivation_path_edit = QLineEdit()
        self.derivation_path_edit.textChanged.connect(self.validate)
        self.layout().addWidget(self.derivation_path_edit)

        on_choice_click(self.choice_w.selected_index)  # set default value for derivation path

        self.layout().addStretch(1)

    def validate(self):
        self.apply()

        cosigner_data = self.wizard.current_cosigner(self.wizard_data)
        derivation_valid = is_bip32_derivation(cosigner_data['derivation_path'])

        if derivation_valid:
            valid, error = self.wizard.check_multisig_constraints(self.wizard_data)
            if not valid:
                # TODO: user feedback
                self.logger.error(error)

        self.valid = valid

    def apply(self):
        cosigner_data = self.wizard.current_cosigner(self.wizard_data)
        cosigner_data['script_type'] = self.choice_w.selected_item[0]
        cosigner_data['derivation_path'] = str(self.derivation_path_edit.text())


class WCCosignerKeystore(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard)

        message = _('Add a cosigner to your multi-sig wallet')
        choices = [
            ('key', _('Enter cosigner key')),
            ('seed', _('Enter cosigner seed')),
            ('hardware', _('Cosign with hardware device'))
        ]

        self.choice_w = ChoiceWidget(message=message, choices=choices)
        self.layout().addWidget(self.choice_w)

        self.cosigner = 0
        self.participants = 0

        self._valid = True

    def on_ready(self):
        self.participants = self.wizard_data['multisig_participants']
        # cosigner index is determined here and put on the wizard_data dict in apply()
        # as this page is the start for each additional cosigner
        self.cosigner = 2 + len(self.wizard_data['multisig_cosigner_data'])

        self.wizard_data['multisig_current_cosigner'] = self.cosigner
        self.title = _("Add Cosigner {}").format(self.wizard_data['multisig_current_cosigner'])

        # different from old wizard: master public key for sharing is now shown on this page
        self.layout().addSpacing(20)
        self.layout().addWidget(WWLabel(_('Below is your master public key. Please share it with your cosigners')))
        slayout = SeedLayout(
            self.wizard_data['multisig_master_pubkey'],
            icon=False,
            for_seed_words=False,
            config=self.wizard.config,
        )
        self.layout().addLayout(slayout)
        self.layout().addStretch(1)

    def apply(self):
        self.wizard_data['cosigner_keystore_type'] = self.choice_w.selected_item[0]
        self.wizard_data['multisig_current_cosigner'] = self.cosigner
        self.wizard_data['multisig_cosigner_data'][str(self.cosigner)] = {}


class WCHaveMasterKey(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Create keystore from a master key'))

        self.message_create = ' '.join([
            _("To create a watching-only wallet, please enter your master public key (xpub/ypub/zpub)."),
            _("To create a spending wallet, please enter a master private key (xprv/yprv/zprv).")
        ])
        self.message_cosign = ' '.join([
            _('Please enter the master public key (xpub) of your cosigner.'),
            _('Enter their master private key (xprv) if you want to be able to sign for them.')
        ])

        self.header_layout = QHBoxLayout()
        self.label = WWLabel()
        self.label.setMinimumWidth(400)
        self.header_layout.addWidget(self.label)

        # TODO: KeysLayout assumes too much in parent, refactor KeysLayout
        # for now, fake parent.next_button.setEnabled
        class Hack:
            def setEnabled(self2, b):
                self.valid = b
            def setToolTip(self2, b):
                pass
        self.next_button = Hack()

    def on_ready(self):
        # if self.wallet_type == 'standard':
        #     v = keystore.is_master_key
        #     self.add_xpub_dialog(title=title, message=message, run_next=self.on_restore_from_key, is_valid=v)
        # else:
        #     i = len(self.keystores) + 1
        #     self.add_cosigner_dialog(index=i, run_next=self.on_restore_from_key, is_valid=keystore.is_bip32_key)
        if self.wizard_data['wallet_type'] == 'standard':
            self.label.setText(self.message_create)
            v = lambda x: bool(keystore.from_master_key(x))
            self.slayout = KeysLayout(parent=self, header_layout=self.header_layout, is_valid=v,
                                      allow_multi=False, config=self.wizard.config)
            self.layout().addLayout(self.slayout)
        elif self.wizard_data['wallet_type'] == 'multisig':
            if 'multisig_current_cosigner' in self.wizard_data:
                self.title = _("Add Cosigner {}").format(self.wizard_data['multisig_current_cosigner'])
                self.label.setText(self.message_cosign)
            else:
                self.wizard_data['multisig_current_cosigner'] = 0
                self.label.setText(self.message_create)
            v = lambda x: keystore.is_bip32_key(x)
            self.slayout = KeysLayout(parent=self, header_layout=self.header_layout, is_valid=v,
                                      allow_multi=False, config=self.wizard.config)
            self.layout().addLayout(self.slayout)

    def apply(self):
        text = self.slayout.get_text()
        cosigner_data = self.wizard.current_cosigner(self.wizard_data)
        cosigner_data['master_key'] = text


class WCMultisig(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Multi-Signature Wallet'))

        def on_m(m):
            m_label.setText(_('Require {0} signatures').format(m))
            cw.set_m(m)
            backup_warning_label.setVisible(cw.m != cw.n)

        def on_n(n):
            n_label.setText(_('From {0} cosigners').format(n))
            cw.set_n(n)
            m_edit.setMaximum(n)
            backup_warning_label.setVisible(cw.m != cw.n)

        backup_warning_label = WWLabel(_('Warning: to be able to restore a multisig wallet, '
                                         'you should include the master public key for each cosigner '
                                         'in all of your backups.'))

        cw = CosignWidget(2, 2)
        m_label = QLabel()
        n_label = QLabel()

        m_edit = QSlider(Qt.Horizontal, self)
        m_edit.setMinimum(1)
        m_edit.setMaximum(2)
        m_edit.setValue(2)
        m_edit.valueChanged.connect(on_m)
        on_m(m_edit.value())

        n_edit = QSlider(Qt.Horizontal, self)
        n_edit.setMinimum(2)
        n_edit.setMaximum(15)
        n_edit.setValue(2)
        n_edit.valueChanged.connect(on_n)
        on_n(n_edit.value())

        grid = QGridLayout()
        grid.addWidget(n_label, 0, 0)
        grid.addWidget(n_edit, 0, 1)
        grid.addWidget(m_label, 1, 0)
        grid.addWidget(m_edit, 1, 1)

        self.layout().addWidget(cw)
        self.layout().addWidget(WWLabel(_('Choose the number of signatures needed to unlock funds in your wallet:')))
        self.layout().addLayout(grid)
        self.layout().addSpacing(2 * char_width_in_lineedit())
        self.layout().addWidget(backup_warning_label)
        self.layout().addStretch(1)

        self.n_edit = n_edit
        self.m_edit = m_edit

        self._valid = True

    def apply(self):
        self.wizard_data['multisig_participants'] = int(self.n_edit.value())
        self.wizard_data['multisig_signatures'] = int(self.m_edit.value())
        self.wizard_data['multisig_cosigner_data'] = {}


class WCImport(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Import Bitcoin Addresses or Private Keys'))
        message = _(
            'Enter a list of Bitcoin addresses (this will create a watching-only wallet), or a list of private keys.')
        header_layout = QHBoxLayout()
        label = WWLabel(message)
        label.setMinimumWidth(400)
        header_layout.addWidget(label)
        header_layout.addWidget(InfoButton(WIF_HELP_TEXT), alignment=Qt.AlignRight)

        # TODO: KeysLayout assumes too much in parent, refactor KeysLayout
        # for now, fake parent.next_button.setEnabled
        class Hack:
            def setEnabled(self2, b):
                self.valid = b
            def setToolTip(self2, b):
                pass
        self.next_button = Hack()

        v = lambda x: keystore.is_address_list(x) or keystore.is_private_key_list(x, raise_on_error=True)
        self.slayout = KeysLayout(parent=self, header_layout=header_layout, is_valid=v,
                                  allow_multi=True, config=self.wizard.config)
        self.layout().addLayout(self.slayout)

    def apply(self):
        text = self.slayout.get_text()
        if keystore.is_address_list(text):
            self.wizard_data['address_list'] = text
        elif keystore.is_private_key_list(text):
            self.wizard_data['private_key_list'] = text


class WCWalletPassword(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Wallet Password'))

        # TODO: PasswordLayout assumes a button, refactor PasswordLayout
        # for now, fake next_button.setEnabled
        class Hack:
            def setEnabled(self2, b):
                self.valid = b
        self.next_button = Hack()

        self.pw_layout = PasswordLayout(
            msg=MSG_ENTER_PASSWORD,
            kind=PW_NEW,
            OK_button=self.next_button,
            # force_disable_encrypt_cb=force_disable_encrypt_cb
        )
        self.pw_layout.encrypt_cb.setChecked(True)
        self.layout().addLayout(self.pw_layout.layout())
        self.layout().addStretch(1)

    def apply(self):
        self.wizard_data['password'] = self.pw_layout.new_password()
        self.wizard_data['encrypt'] = self.pw_layout.encrypt_cb.isChecked()


class SeedExtensionEdit(QWidget):
    def __init__(self, parent, *, message: str = None, warning: str = None, warn_issue4566: bool = False):
        super().__init__(parent)

        self.warn_issue4566 = warn_issue4566

        layout = QVBoxLayout()
        self.setLayout(layout)

        if message:
            layout.addWidget(WWLabel(message))

        self.line = QLineEdit()
        layout.addWidget(self.line)

        def f(text):
            if self.warn_issue4566:
                text_whitespace_normalised = ' '.join(text.split())
                warn_issue4566_label.setVisible(text != text_whitespace_normalised)
        self.line.textEdited.connect(f)

        if warning:
            layout.addWidget(WWLabel(warning))

        warn_issue4566_label = WWLabel(MSG_PASSPHRASE_WARN_ISSUE4566)
        warn_issue4566_label.setVisible(False)
        layout.addWidget(warn_issue4566_label)

        # expose textEdited signal and text() func to widget
        self.textEdited = self.line.textEdited
        self.text = self.line.text


class CosignWidget(QWidget):
    def __init__(self, m, n):
        QWidget.__init__(self)
        self.size = max(120, 9 * font_height())
        self.R = QRect(0, 0, self.size, self.size)
        self.setGeometry(self.R)
        self.setMinimumHeight(self.size)
        self.setMaximumHeight(self.size)
        self.m = m
        self.n = n

    def set_n(self, n):
        self.n = n
        self.update()

    def set_m(self, m):
        self.m = m
        self.update()

    def paintEvent(self, event):
        bgcolor = self.palette().color(QPalette.Background)
        pen = QPen(bgcolor, 7, Qt.SolidLine)
        qp = QPainter()
        qp.begin(self)
        qp.setPen(pen)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.setBrush(Qt.gray)
        for i in range(self.n):
            alpha = int(16 * 360 * i/self.n)
            alpha2 = int(16 * 360 * 1/self.n)
            qp.setBrush(Qt.green if i < self.m else Qt.gray)
            qp.drawPie(self.R, alpha, alpha2)
        qp.end()


class WCChooseHWDevice(WizardComponent, Logger):
    scanFailed = pyqtSignal([str, str], arguments=['code', 'message'])
    scanComplete = pyqtSignal()

    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Choose Hardware Device'))
        Logger.__init__(self)
        self.scanFailed.connect(self.on_scan_failed)
        self.scanComplete.connect(self.on_scan_complete)
        self.plugins = wizard.plugins

        self.error_l = WWLabel()
        self.error_l.setVisible(False)

        self.device_list = QWidget()
        self.device_list_layout = QVBoxLayout()
        self.device_list.setLayout(self.device_list_layout)
        self.choice_w = None

        self.rescan_button = QPushButton(_('Rescan devices'))
        self.rescan_button.clicked.connect(self.on_rescan)

        self.layout().addWidget(self.error_l)
        self.layout().addWidget(self.device_list)
        self.layout().addStretch(1)
        self.layout().addWidget(self.rescan_button)

        self.c_values = []

    def on_ready(self):
        self.scan_devices()

    def on_rescan(self):
        self.scan_devices()

    def on_scan_failed(self, code, message):
        self.error_l.setText(message)
        self.error_l.setVisible(True)
        self.device_list.setVisible(False)

        self.valid = False

    def on_scan_complete(self):
        self.error_l.setVisible(False)
        self.device_list.setVisible(True)

        choices = []
        for name, info in self.devices:
            state = _("initialized") if info.initialized else _("wiped")
            label = info.label or _("An unnamed {}").format(name)
            try:
                transport_str = info.device.transport_ui_string[:20]
            except Exception:
                transport_str = 'unknown transport'
            descr = f"{label} [{info.model_name or name}, {state}, {transport_str}]"
            choices.append(((name, info), descr))
        msg = _('Select a device') + ':'

        # remove old component before adding anew
        a = self.device_list.layout().itemAt(0)
        self.device_list.layout().removeItem(a)

        self.choice_w = ChoiceWidget(message=msg, choices=choices)
        self.device_list.layout().addWidget(self.choice_w)

        self.valid = True

    def failed_getting_device_infos(self, debug_msg, name, e):
        # nonlocal debug_msg
        err_str_oneline = ' // '.join(str(e).splitlines())
        self.logger.warning(f'error getting device infos for {name}: {err_str_oneline}')
        indented_error_msg = '    '.join([''] + str(e).splitlines(keepends=True))
        debug_msg += f'  {name}: (error getting device infos)\n{indented_error_msg}\n'

    def scan_devices(self):
        self.valid = False
        self.busy_msg = _('Scanning devices...')
        self.busy = True

        def scan_task():
            # check available plugins
            supported_plugins = self.plugins.get_hardware_support()
            devices = []  # type: List[Tuple[str, DeviceInfo]]
            devmgr = self.plugins.device_manager
            debug_msg = ''

            # scan devices
            try:
                # scanned_devices = self.run_task_without_blocking_gui(task=devmgr.scan_devices,
                #                                                      msg=_("Scanning devices..."))
                scanned_devices = devmgr.scan_devices()
            except BaseException as e:
                self.logger.info('error scanning devices: {}'.format(repr(e)))
                debug_msg = '  {}:\n    {}'.format(_('Error scanning devices'), e)
            else:
                for splugin in supported_plugins:
                    name, plugin = splugin.name, splugin.plugin
                    # plugin init errored?
                    if not plugin:
                        e = splugin.exception
                        indented_error_msg = '    '.join([''] + str(e).splitlines(keepends=True))
                        debug_msg += f'  {name}: (error during plugin init)\n'
                        debug_msg += '    {}\n'.format(_('You might have an incompatible library.'))
                        debug_msg += f'{indented_error_msg}\n'
                        continue
                    # see if plugin recognizes 'scanned_devices'
                    try:
                        # FIXME: side-effect: this sets client.handler
                        device_infos = devmgr.list_pairable_device_infos(
                            handler=None, plugin=plugin, devices=scanned_devices, include_failing_clients=True)
                    except HardwarePluginLibraryUnavailable as e:
                        self.failed_getting_device_infos(debug_msg, name, e)
                        continue
                    except BaseException as e:
                        self.logger.exception('')
                        self.failed_getting_device_infos(debug_msg, name, e)
                        continue
                    device_infos_failing = list(filter(lambda di: di.exception is not None, device_infos))
                    for di in device_infos_failing:
                        self.failed_getting_device_infos(debug_msg, name, di.exception)
                    device_infos_working = list(filter(lambda di: di.exception is None, device_infos))
                    devices += list(map(lambda x: (name, x), device_infos_working))
            if not debug_msg:
                debug_msg = '  {}'.format(_('No exceptions encountered.'))
            if not devices:
                msg = (_('No hardware device detected.') + '\n' +
                       _('To trigger a rescan, press \'Rescan devices\'.') + '\n\n')
                if sys.platform == 'win32':
                    msg += _('If your device is not detected on Windows, go to "Settings", "Devices", "Connected devices", '
                             'and do "Remove device". Then, plug your device again.') + '\n'
                    msg += _('While this is less than ideal, it might help if you run Electrum as Administrator.') + '\n'
                else:
                    msg += _('On Linux, you might have to add a new permission to your udev rules.') + '\n'
                msg += '\n\n'
                msg += _('Debug message') + '\n' + debug_msg

                self.scanFailed.emit('no_devices', msg)
                self.busy = False
                return

            # select device
            self.devices = devices
            self.scanComplete.emit()
            self.busy = False

        t = threading.Thread(target=scan_task, daemon=True)
        t.start()

    def apply(self):
        if self.choice_w:
            # TODO: data is not (de)serializable yet, wizard_data cannot be persisted
            self.wizard_data['hardware_device'] = self.choice_w.selected_item[0]


class WCWalletPasswordHardware(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Encrypt using hardware'))
        self.plugins = wizard.plugins

        self.playout = PasswordLayoutForHW(MSG_HW_STORAGE_ENCRYPTION)
        self.playout.encrypt_cb.setChecked(True)
        self.layout().addLayout(self.playout.layout())
        self.layout().addStretch(1)

        self._valid = True

    def apply(self):
        self.wizard_data['encrypt'] = self.playout.encrypt_cb.isChecked()
        if self.playout.encrypt_cb.isChecked():
            _name, _info = self.wizard_data['hardware_device']
            device_id = _info.device.id_
            client = self.plugins.device_manager.client_by_id(device_id, scan_now=False)
            # client.handler = self.plugin.create_handler(self.wizard)
            self.wizard_data['password'] = client.get_password_for_storage_encryption()


class WCHWUnlock(WizardComponent, Logger):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Unlocking hardware'))
        Logger.__init__(self)
        self.plugins = wizard.plugins
        self.plugin = None
        self._busy = True
        self.password = None

        self.ok_l = WWLabel(_('Hardware successfully unlocked'))
        self.ok_l.setAlignment(Qt.AlignCenter)
        self.layout().addWidget(self.ok_l)

    def on_ready(self):
        _name, _info = self.wizard_data['hardware_device']
        self.plugin = self.plugins.get_plugin(_info.plugin_name)
        self.title = _('Unlocking {} ({})').format(_info.model_name, _info.label)

        device_id = _info.device.id_
        client = self.plugins.device_manager.client_by_id(device_id, scan_now=False)
        client.handler = self.plugin.create_handler(self.wizard)

        def unlock_task(client):
            try:
                self.password = client.get_password_for_storage_encryption()
            except Exception as e:
                self.error = repr(e)  # TODO: handle user interaction exceptions (e.g. invalid pin) more gracefully
                self.logger.error(repr(e))
            self.busy = False
            self.validate()

        t = threading.Thread(target=unlock_task, args=(client,), daemon=True)
        t.start()

    def validate(self):
        if self.password and not self.error:
            self.apply()
            self.valid = True
        else:
            self.valid = False

        if self.valid:
            self.wizard.requestNext.emit()  # via signal, so it triggers Next/Finish on GUI thread after on_updated()

    def apply(self):
        if self.valid:
            self.wizard_data['password'] = self.password


class WCHWXPub(WizardComponent, Logger):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Retrieving extended public key from hardware'))
        Logger.__init__(self)
        self.plugins = wizard.plugins
        self.plugin = None
        self._busy = True

        self.xpub = None
        self.root_fingerprint = None
        self.label = None
        self.soft_device_id = None

        self.ok_l = WWLabel(_('Hardware keystore added to wallet'))
        self.ok_l.setAlignment(Qt.AlignCenter)
        self.layout().addWidget(self.ok_l)

    def on_ready(self):
        _name, _info = self.wizard_data['hardware_device']
        self.plugin = self.plugins.get_plugin(_info.plugin_name)
        self.title = _('Retrieving extended public key from {} ({})').format(_info.model_name, _info.label)

        device_id = _info.device.id_
        client = self.plugins.device_manager.client_by_id(device_id, scan_now=False)
        if not client.handler:
            client.handler = self.plugin.create_handler(self.wizard)

        cosigner = self.wizard.current_cosigner(self.wizard_data)
        xtype = cosigner['script_type']
        derivation = cosigner['derivation_path']

        def get_xpub_task(client, derivation, xtype):
            try:
                self.xpub = self.get_xpub_from_client(client, derivation, xtype)
                self.root_fingerprint = client.request_root_fingerprint_from_device()
                self.label = client.label()
                self.soft_device_id = client.get_soft_device_id()
            except UserFacingException as e:
                self.error = str(e)
                self.logger.error(repr(e))
            except Exception as e:
                self.error = repr(e)  # TODO: handle user interaction exceptions (e.g. invalid pin) more gracefully
                self.logger.error(repr(e))
            self.logger.debug(f'Done retrieve xpub: {self.xpub}')
            self.busy = False
            self.validate()

        t = threading.Thread(target=get_xpub_task, args=(client, derivation, xtype), daemon=True)
        t.start()

    def get_xpub_from_client(self, client, derivation, xtype):  # override for HWW specific client if needed
        _name, _info = self.wizard_data['hardware_device']
        if xtype not in self.plugin.SUPPORTED_XTYPES:
            raise ScriptTypeNotSupported(_('This type of script is not supported with {}').format(_info.model_name))
        return client.get_xpub(derivation, xtype)

    def validate(self):
        if self.xpub and not self.error:
            self.apply()
            valid, error = self.wizard.check_multisig_constraints(self.wizard_data)
            if not valid:
                self.error = '\n'.join([
                    _('Could not add hardware keystore to wallet'),
                    error
                ])
            self.valid = valid
        else:
            self.valid = False

        if self.valid:
            self.wizard.requestNext.emit()  # via signal, so it triggers Next/Finish on GUI thread after on_updated()

    def apply(self):
        _name, _info = self.wizard_data['hardware_device']
        cosigner_data = self.wizard.current_cosigner(self.wizard_data)
        cosigner_data['hw_type'] = _info.plugin_name
        cosigner_data['master_key'] = self.xpub
        cosigner_data['root_fingerprint'] = self.root_fingerprint
        cosigner_data['label'] = self.label
        cosigner_data['soft_device_id'] = self.soft_device_id


class WCHWUninitialized(WizardComponent):
    def __init__(self, parent, wizard):
        WizardComponent.__init__(self, parent, wizard, title=_('Hardware not initialized'))

    def on_ready(self):
        _name, _info = self.wizard_data['hardware_device']
        self.layout().addWidget(WWLabel(_('This {} is not initialized. Cannot continue').format(_info.model_name)))
