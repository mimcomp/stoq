# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2005-2008 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s):   Evandro Vale Miquelito      <evandro@async.com.br>
##              Johan Dahlin                <jdahlin@async.com.br>
##              George Kussumoto            <george@async.com.br>
##
##
""" Slaves for payment management """

import gtk

from decimal import Decimal
import datetime
from dateutil.relativedelta import relativedelta

from kiwi import ValueUnset
from kiwi.datatypes import format_price, currency, ValidationError
from kiwi.component import get_utility
from kiwi.python import Settable
from kiwi.utils import gsignal
from kiwi.ui.objectlist import Column
from kiwi.ui.views import SlaveView

from stoqlib.database.runtime import get_connection
from stoqlib.domain.account import BankAccount
from stoqlib.domain.events import CreatePaymentEvent
from stoqlib.domain.interfaces import IInPayment, IOutPayment
from stoqlib.domain.payment.group import PaymentGroup
from stoqlib.domain.payment.method import (CheckData, PaymentMethod,
                                           CreditCardData)
from stoqlib.domain.payment.payment import Payment, PaymentChangeHistory
from stoqlib.domain.payment.renegotiation import PaymentRenegotiation
from stoqlib.domain.person import PersonAdaptToCreditProvider
from stoqlib.domain.purchase import PurchaseOrder
from stoqlib.domain.sale import Sale
from stoqlib.drivers.cheque import get_current_cheque_printer_settings
from stoqlib.enums import CreatePaymentStatus
from stoqlib.gui.base.dialogs import run_dialog
from stoqlib.gui.editors.baseeditor import BaseEditorSlave, BaseEditor
from stoqlib.gui.interfaces import IDomainSlaveMapper
from stoqlib.lib.defaults import (interval_types, INTERVALTYPE_MONTH,
     DECIMAL_PRECISION, calculate_interval)
from stoqlib.lib.message import info
from stoqlib.lib.translation import stoqlib_gettext

_ = stoqlib_gettext


class _TemporaryBillData(object):
    def __init__(self, group=None, first_duedate=None):
        self.group = group
        self.first_duedate = first_duedate
        self.installments_number = 1
        self.intervals = 1
        self.interval_type = 0


class _TemporaryMoneyData(object):
    def __init__(self):
        self.first_duedate = datetime.datetime.today()
        self.installments_number = 1
        self.intervals = 1


class _TemporaryStoreCreditData(object):
    def __init__(self):
        self.first_duedate = datetime.datetime.today()
        self.installments_number = 1
        self.intervals = 1


class _TemporaryCreditProviderGroupData(object):
    def __init__(self, group, provider):
        self.installments_number = 1
        self.provider = provider
        self.group = group


class PaymentListSlave(BaseEditorSlave):
    """A basic payment list slave. Each element of this list is a payment
    method slave which hold informations about payments. Available slaves
    are: BillDataSlave and CheckDataSlave

    Notes:
        - get_payment_slave: is a hook method which must be defined in
                             parents. The result of this function must
                             be a BaseEditorSlave instance.
    """

    gladefile = 'PaymentListSlave'
    model_type = PaymentMethod

    gsignal('remove-slave')
    gsignal('add-slave')
    gsignal('remove-item', SlaveView)

    def __init__(self, parent, conn, payment_method, total_amount):
        self.parent = parent
        self.total_amount = total_amount
        self.max_installments = None
        # This dict stores a reference of each toplevel widget with its own
        # kiwi object, the slave.
        self.payment_slaves = {}
        BaseEditorSlave.__init__(self, conn, payment_method)
        self.update_view()

    #
    # Private
    #

    def _remove_payment_slave(self, widget):
        slave = self.payment_slaves[widget]
        del self.payment_slaves[widget]
        self.list_vbox.remove(widget)
        self.update_view()
        self.emit("remove-item", slave)

    def _remove_last_payment_slave(self):
        vbox_children = self.list_vbox.get_children()
        if not vbox_children:
            return
        widget = vbox_children[-1]
        self._remove_payment_slave(widget)

    #
    # Public API
    #

    def get_total_difference(self):
        """Get the difference for the total of check payments invoiced. If
        the difference is zero the entire sale total value is invoiced.
        If the difference is greater than zero, there is an outstanding
        amount to invoice. If the value is negative, there is a overpaid
        value.
        """
        slaves = self.payment_slaves.values()
        values = [s.get_payment_value() for s in slaves
                        if s.get_payment_value() is not None]
        total = sum(values, currency(0))
        slaves_total = Decimal(str(total))
        slaves_total -= self.parent.get_interest_total()
        if slaves_total == self.total_amount:
            return currency(0)
        return currency(self.total_amount - slaves_total)

    def update_view(self):
        children_number = self.get_children_number()
        can_remove = children_number > 1
        max = self.max_installments or 0
        can_add = children_number < max
        self.remove_button.set_sensitive(can_remove)
        self.add_button.set_sensitive(can_add)
        self.update_total_label()

    def update_total_label(self):
        difference = self.get_total_difference()
        if not round(difference, DECIMAL_PRECISION):
            label_name = difference = ''
        elif difference < 0:
            difference *= -1
            label_name = _('Overpaid:')
        else:
            label_name = _('Outstanding:')
        if difference:
            difference = format_price(difference)
        self.total_label.set_text(difference)
        self.status_label.set_text(label_name)

    def get_children_number(self):
        vbox_children = self.list_vbox.get_children()
        return len(vbox_children)

    def register_max_installments(self, inst_number):
        self.max_installments = inst_number

    def clear_list(self):
        for widget in self.list_vbox.get_children()[:]:
            self._remove_payment_slave(widget)

    def update_payment_list(self, installments_number):
        installments_number = installments_number or 0
        children_number = self.get_children_number()
        difference = installments_number - children_number
        if not difference:
            return
        if difference > 0:
            for unused in range(difference):
                self.add_slave()
        else:
            difference *= -1
            for unused in range(difference):
                self._remove_last_payment_slave()

    def add_slave(self, slave=None):
        if not self.max_installments:
            raise ValueError('You call register_max_installments '
                             'before start adding slaves')
        if self.get_children_number() > self.max_installments:
            return
        slave = slave or self.parent.get_payment_slave()
        widget = slave.get_toplevel()
        self.payment_slaves[widget] = slave
        children_number = self.get_children_number() + 1
        slave.set_frame_label('# %d' % children_number)
        self.list_vbox.pack_start(widget, False)
        # Scroll to the bottom of the scrolled window
        vadj = self.scrolled_window.get_vadjustment()
        vadj.set_value(vadj.upper)
        widget.show()
        self.update_view()

    def is_all_due_dates_valid(self):
        today = datetime.date.today()
        for slave in self.payment_slaves.values():
            if slave.due_date.read() < today:
                return False
        return True

    #
    # Kiwi callbacks
    #

    def on_add_button__clicked(self, *args):
        self.add_slave()
        self.emit('add-slave')

    def on_remove_button__clicked(self, *args):
        self._remove_last_payment_slave()
        self.emit('remove-slave')


class BankDataSlave(BaseEditorSlave):
    """  A simple slave that contains only a hbox with fields to bank name and
    its branch. This slave is used by payment method slaves that has reference
    to a BankAccount object.
    """
    gladefile = 'BankDataSlave'
    model_type = BankAccount
    proxy_widgets = ('bank', 'branch')

    #
    # BaseEditorSlave hooks
    #

    def setup_proxies(self):
        self.add_proxy(self.model, BankDataSlave.proxy_widgets)


class BillDataSlave(BaseEditorSlave):
    """ A slave to set payment information of bill payment method.
    """

    gladefile = 'BillDataSlave'
    model_type = Payment
    payment_widgets = ('due_date', 'value', 'payment_number')
    gsignal('paymentvalue-changed')
    gsignal('duedate-validate')

    def __init__(self, conn, payment_group, due_date, value,
                 method_iface, model=None):
        self._payment_group = payment_group
        self._due_date = due_date
        self._value = value
        self._method_iface = method_iface
        BaseEditorSlave.__init__(self, conn, model)

    def _setup_widgets(self):
        self.payment_number_label.set_bold(True)
        self.payment_number_label.set_size('small')

    def set_frame_label(self, label_name):
        self.payment_number_label.set_text(label_name)

    def get_payment_value(self):
        return self.model.value

    #
    # BaseEditorSlave hooks
    #

    def create_model(self, conn):
        bill_method = PaymentMethod.get_by_name(conn, 'bill')
        apayment = bill_method.create_payment(self._method_iface,
                                              self._payment_group,
                                              self._value,
                                              self._due_date)
        return apayment.get_adapted()

    def setup_proxies(self):
        self._setup_widgets()
        self.add_proxy(self.model, BillDataSlave.payment_widgets)

    #
    # Kiwi callbacks
    #

    def after_value__changed(self, *args):
        self.emit('paymentvalue-changed')

    def on_due_date__validate(self, widget, value):
        self.emit('duedate-validate')
        if value < datetime.date.today():
            return ValidationError(_(u"Expected installment due date "
                                      "must be set to a future date"))


class CheckDataSlave(BillDataSlave):
    """A slave to set payment information of check payment method."""
    slave_holder = 'bank_data_slave'
    model_type = CheckData

    def __init__(self, conn, payment_group, due_date, value,
                 is_sale_payment, model=None, default_bank=None):
        self._default_bank = default_bank
        BillDataSlave.__init__(self, conn, payment_group, due_date,
                               value, is_sale_payment, model)

    #
    # BaseEditorSlave hooks
    #

    def get_payment_value(self):
        return self.model.payment.value

    def create_model(self, conn):
        check_method = PaymentMethod.get_by_name(conn, 'check')
        payment = check_method.create_payment(self._method_iface,
                                              self._payment_group,
                                              self._value, self._due_date)
        return check_method.operation.get_check_data_by_payment(
            payment.get_adapted())

    def setup_slaves(self):
        if self._default_bank and not self.model.bank_data.bank_id:
            self.model.bank_data.bank_id = self._default_bank
        bank_data_slave = BankDataSlave(self.conn, self.model.bank_data)
        if self.get_slave(self.slave_holder):
            self.detach_slave(self.slave_holder)
        self.attach_slave(self.slave_holder, bank_data_slave)

    def setup_proxies(self):
        self._setup_widgets()
        self.add_proxy(self.model.payment, BillDataSlave.payment_widgets)


class BasePaymentMethodSlave(BaseEditorSlave):
    """A base payment method slave for Bill and Check methods."""

    gladefile = 'BillCheckMethodSlave'
    model_type = _TemporaryBillData
    slave_holder = 'bill_check_data_list'
    proxy_widgets = ('interval_type_combo',
                     'intervals',
                     'first_duedate',
                     'installments_number')
    # This attribute must be defined in child. It can assume two
    # value: CheckDataSlave, BillDataSlave
    _data_slave_class = None

    def __init__(self, wizard, parent, conn, order_obj, payment_method,
                 outstanding_value=currency(0)):
        # Note that 'order' may be a Sale or a PurchaseOrder object
        self.order = order_obj
        self.wizard = wizard
        self.method = payment_method
        self.method_iface = self._get_payment_method_iface()
        # This is very useful when calculating the total amount outstanding
        # or overpaid of the payments
        self.interest_total = currency(0)
        self.payment_group = self.order.group
        self.payment_list = None
        self._reset_btn_validation_ok = True
        self.total_value = outstanding_value or self._get_total_amount()
        BaseEditorSlave.__init__(self, conn)
        self.register_validate_function(self._refresh_next)
        self.parent = parent
        self.interval_type_combo.set_sensitive(False)
        self.intervals.set_sensitive(False)
        self.update_view()

    def _refresh_next(self, validation_ok=True):
        if validation_ok and self.payment_list:
            total_difference = self.payment_list.get_total_difference()
            validation_ok = (total_difference == currency(0) and
                             self.payment_list.is_all_due_dates_valid())
        self.wizard.refresh_next(validation_ok)

    def update_view(self):
        attrs = [self.model.installments_number, self.model.first_duedate,
                 self.model.intervals]
        self.reset_button.set_sensitive((None not in attrs) and
                                        self._reset_btn_validation_ok)
        self._refresh_next()

    def _setup_widgets(self):
        max = self.method.max_installments
        self.installments_number.set_range(1, max)
        self.installments_number.set_value(1)

        items = [(label, constant) for constant, label
                                in interval_types.items()]
        self.interval_type_combo.prefill(items)
        self.payment_list = PaymentListSlave(self, self.conn,
                                             self.method, self.total_value)
        self.payment_list.connect('add-slave',
                                  self.update_installments_number)
        self.payment_list.connect('remove-slave',
                                  self.update_installments_number)
        self.payment_list.connect("remove-item",
                                  self._on_payment_list__remove_item)
        self.payment_list.register_max_installments(max)
        if self.get_slave(BasePaymentMethodSlave.slave_holder):
            self.detach_slave(BasePaymentMethodSlave.slave_holder)
        self.attach_slave(BasePaymentMethodSlave.slave_holder,
                          self.payment_list)
        created_adapted_payments = self.get_created_adapted_payments()
        if created_adapted_payments:
            self.fill_slave_list(created_adapted_payments)
        else:
            # Adding the first payment
            slave = self.get_payment_slave()
            self.payment_list.add_slave(slave)

    def get_created_adapted_payments(self):
        for payment in Payment.selectBy(group=self.payment_group,
                                        method=self.method,
                                        status=Payment.STATUS_PREVIEW,
                                        connection=self.conn):
            yield self.method_iface(payment, None)

    def _get_total_amount(self):
        """Returns the order total amount """
        if isinstance(self.order, Sale):
            return self.order.get_total_sale_amount()
        elif isinstance(self.order, PurchaseOrder):
            return self.order.get_purchase_total()
        elif isinstance(self.order, PaymentRenegotiation):
            return self.order.total
        else:
            raise TypeError

    def _get_payment_method_iface(self):
        if isinstance(self.order, Sale):
            return IInPayment
        elif isinstance(self.order, PurchaseOrder):
            return IOutPayment
        elif isinstance(self.order, PaymentRenegotiation):
            return IInPayment
        else:
            raise TypeError

    #
    # General methods
    #

    def _setup_payments(self):
        self.payment_list.clear_list()
        due_dates = []
        interval = calculate_interval(self.model.interval_type,
                                      self.model.intervals)
        installments_number = self.model.installments_number
        self.payment_group.installments_number = installments_number
        due_date = self.model.first_duedate
        for i in range(installments_number):
            #XXX: convert to datetime.datetime
            d = datetime.datetime(due_date.year, due_date.month, due_date.day)
            due_dates.append(d + datetime.timedelta(i * interval))

        payments = self.method.create_payments(self.method_iface,
                                               self.payment_group,
                                               self.total_value,
                                               due_dates)
        interest = Decimal(0)

        # This is very useful when calculating the total amount outstanding
        # or overpaid of the payments
        self.interest_total = interest
        self.fill_slave_list(payments)

    def fill_slave_list(self, adapted_payments):
        for adapted in adapted_payments:
            slave = self.get_slave_by_adapted_payment(adapted)
            self.payment_list.add_slave(slave)

    def get_slave_by_adapted_payment(self, adapted_payment):
        raise NotImplementedError

    def get_interest_total(self):
        return self.interest_total

    def get_extra_slave_args(self):
        """  This method can be redefined in child when extra parameters needs
        to be passed to the slave class. This method must return always a list
        with the parameters.
        """
        return []

    #
    # PaymentListSlave
    #

    def get_payment_slave(self, model=None):
        if not self._data_slave_class:
            raise ValueError('Child classes must define a data_slave_class '
                             'attribute')
        due_date = datetime.datetime.today()
        if not self.payment_list.get_children_number():
            total = self.total_value
        else:
            total = currency(0)
        extra_params = self.get_extra_slave_args()
        slave = self._data_slave_class(self.conn, self.payment_group, due_date,
                                       total, self.method_iface, model,
                                       *extra_params)
        slave.connect('paymentvalue-changed',
                      self._on_slave__paymentvalue_changed)
        slave.connect('duedate-validate',
                      self._on_slave__duedate_validate)
        return slave

    def update_installments_number(self, *args):
        inst_number = self.payment_list.get_children_number()
        self.model.installments_number = inst_number
        self.proxy.update('installments_number')

    #
    # PaymentMethodStep hooks
    #

    def finish(self):
        # Since payments are created during this step there is no need to
        # perform tasks here
        return

    #
    # BaseEditor Slave hooks
    #

    def setup_proxies(self):
        self._setup_widgets()
        self.proxy = self.add_proxy(self.model,
                                    BasePaymentMethodSlave.proxy_widgets)
        self.interval_type_combo.select_item_by_data(INTERVALTYPE_MONTH)

    def create_model(self, conn):
        return _TemporaryBillData(group=self.payment_group,
                        first_duedate=datetime.datetime.today())

    #
    # Kiwi callbacks
    #

    def on_installments_number__changed(self, proxyspinbutton):
        # Call this callback *on* the value changed because we need to
        # have the same value for the length of the payments list and
        # validate the installments_number
        inst_number = self.model.installments_number
        max = self.method.max_installments
        if inst_number > max:
            self.installments_number.set_invalid(_("The number of installments "
                "must be less then %d" % max))
            self._refresh_next(False)
            return
        if self.payment_list:
            self.payment_list.update_payment_list(inst_number)
        has_installments = inst_number > 1
        self.interval_type_combo.set_sensitive(has_installments)
        self.intervals.set_sensitive(has_installments)
        self._refresh_next(False)

    def on_first_duedate__validate(self, widget, value):
        if value < datetime.date.today():
            return ValidationError(_("Expected first installment date must be set "
                "to a future date"))
        self._refresh_next(False)

    def on_intervals__value_changed(self, *args):
        self.update_view()
        self._refresh_next(False)

    def on_interval_type_combo__changed(self, *args):
        self.update_view()
        self._refresh_next(False)

    def on_reset_button__clicked(self, *args):
        self._setup_payments()
        self.update_view()

    def on_intervals__validation_changed(self, widget, is_valid):
        self._reset_btn_validation_ok = is_valid
        self.update_view()

    def on_first_duedate__validation_changed(self, widget, is_valid):
        self._reset_btn_validation_ok = is_valid
        self.update_view()

    def on_installments_number__validation_changed(self, widget, is_valid):
        self._reset_btn_validation_ok = is_valid
        self.update_view()

    def _on_slave__paymentvalue_changed(self, slave):
        self.update_view()
        self.payment_list.update_total_label()

    def _on_slave__duedate_validate(self, slave):
        self.update_view()

    def _on_payment_list__remove_item(self, payment_list, slave):
        if not isinstance(slave.model, slave.model_type):
            raise TypeError('Slave model attribute should be of type '
                            '%s, got %s' % (slave.model_type,
                                            type(slave.model)))

        if isinstance(slave.model, CheckData):
            payment = slave.model.payment
        else:
            payment = slave.model

        Payment.delete(payment.id, self.conn)


class CheckMethodSlave(BasePaymentMethodSlave):
    _data_slave_class = CheckDataSlave

    def get_slave_by_adapted_payment(self, adapted_payment):
        check_data = self.method.operation.get_check_data_by_payment(
            adapted_payment.get_adapted())
        return self.get_payment_slave(check_data)

    def get_extra_slave_args(self):
        """ If there is any selected item in the banks combo, return this
        as extra parameter to the slave (CheckDataSlave). """
        if (self.bank_combo.get_property("visible")
            and len(self.bank_combo.get_model())):
            bank_id = self.bank_combo.get_selected()
            if bank_id:
                return [bank_id]
        return []

    def _setup_widgets(self):
        printer = get_current_cheque_printer_settings(self.conn)
        if not printer:
            self.bank_combo.hide()
            self.bank_label.hide()
        else:
            banks = printer.get_banks()
            items = [("%s - %s" % (code, bank.name), code)
                         for code, bank in banks.items()]
            self.bank_combo.prefill(items)
        BasePaymentMethodSlave._setup_widgets(self)


class BillMethodSlave(BasePaymentMethodSlave):
    _data_slave_class = BillDataSlave

    def __init__(self, wizard, parent, conn, sale, payment_method,
                 outstanding_value=currency(0)):
        BasePaymentMethodSlave.__init__(self, wizard, parent, conn,
                                        sale, payment_method,
                                        outstanding_value=outstanding_value)
        self.bank_label.hide()
        self.bank_combo.hide()

    def get_slave_by_adapted_payment(self, adapted_payment):
        payment = adapted_payment.get_adapted()
        return self.get_payment_slave(payment)


class _MoneyData(BillDataSlave):

    def create_model(self, conn):
        money_method = PaymentMethod.get_by_name(conn, 'money')
        apayment = money_method.create_payment(self._method_iface,
                                               self._payment_group,
                                               self._value,
                                               self._due_date)
        return apayment.get_adapted()


class _StoreCreditData(BillDataSlave):

    def create_model(self, conn):
        method = PaymentMethod.get_by_name(conn, 'store_credit')
        apayment = method.create_payment(self._method_iface,
                                         self._payment_group,
                                         self._value,
                                         self._due_date)
        return apayment.get_adapted()


class MoneyMethodSlave(BasePaymentMethodSlave):
    model_type = _TemporaryMoneyData
    _data_slave_class = _MoneyData

    def __init__(self, wizard, parent, conn, total_amount,
                 payment_method, outstanding_value=currency(0)):
        BasePaymentMethodSlave.__init__(self, wizard, parent, conn,
                                        total_amount, payment_method,
                                        outstanding_value=outstanding_value)
        self.bank_label.hide()
        self.bank_combo.hide()
        self.first_duedate_lbl.hide()
        self.first_duedate.hide()

    def get_slave_by_adapted_payment(self, adapted_payment):
        return self.get_payment_slave(adapted_payment.get_adapted())

    def create_model(self, conn):
        return _TemporaryMoneyData()


class StoreCreditMethodSlave(BasePaymentMethodSlave):
    model_type = _TemporaryStoreCreditData
    _data_slave_class = _StoreCreditData

    def __init__(self, wizard, parent, conn, total_amount,
                 payment_method, outstanding_value=currency(0)):
        BasePaymentMethodSlave.__init__(self, wizard, parent, conn,
                                        total_amount, payment_method,
                                        outstanding_value=outstanding_value)
        self.bank_label.hide()
        self.bank_combo.hide()
        self.first_duedate_lbl.hide()
        self.first_duedate.hide()

    def get_slave_by_adapted_payment(self, adapted_payment):
        return self.get_payment_slave(adapted_payment.get_adapted())

    def create_model(self, conn):
        return _TemporaryStoreCreditData()


class CardMethodSlave(BaseEditorSlave):
    """A base payment method slave for card and finance methods.
    Available slaves are: CardMethodSlave
    """
    gladefile = 'CreditProviderMethodSlave'
    model_type = _TemporaryCreditProviderGroupData
    proxy_widgets = ('credit_provider',
                     'installments_number')

    def __init__(self, wizard, parent, conn, order, payment_method,
                 outstanding_value=currency(0)):
        self.model = order
        self.wizard = wizard
        self.method = payment_method
        self._payment_group = self.model.group
        self.total_value = (outstanding_value or
                            self._get_total_amount())
        self.providers = self._get_credit_providers()
        self._selected_type = CreditCardData.TYPE_CREDIT
        BaseEditorSlave.__init__(self, conn)
        self.parent = parent
        self._order = order

        # this will change after the payment type is changed
        self.installments_number.set_range(1, 1)
        self._refresh_next(False)

    #
    # PaymentMethodStep hooks
    #

    def finish(self):
        self._setup_payments()

    def update_view(self):
        pass

    #
    # BaseEditor Slave hooks
    #

    def setup_proxies(self):
        self._setup_widgets()
        self.proxy = self.add_proxy(self.model, self.proxy_widgets)

        # Workaround for a kiwi bug. report me
        self.credit_provider.select_item_by_position(1)
        self.credit_provider.select_item_by_position(0)

    def create_model(self, conn):
        if not self.providers:
            raise ValueError('You must have credit providers information '
                             'stored in the database before start doing '
                             'sales')
        return _TemporaryCreditProviderGroupData(
            group=self._payment_group,
            provider=None)

    # Private

    def _get_total_amount(self):
        if isinstance(self.model, Sale):
            return self.model.get_total_sale_amount()
        elif isinstance(self.model, PaymentRenegotiation):
            return self.model.total
        else:
            raise TypeError

    def _setup_widgets(self):
        provider_items = [(p.short_name, p) for p in self.providers]
        self.credit_provider.prefill(provider_items)

        self._radio_group = None

        for ptype, name in CreditCardData.types.items():
            self._add_card_type(name, ptype)

    def _add_card_type(self, name, payment_type):
        radio = gtk.RadioButton(self._radio_group, name)
        radio.set_data('type', payment_type)
        radio.connect('toggled', self._on_card_type_radio_toggled)
        self.types_box.pack_start(radio)
        radio.show()

        if self._radio_group is None:
            self._radio_group = radio

    def _on_card_type_radio_toggled(self, radio):
        if not radio.get_active():
            return

        self._selected_type = radio.get_data('type')
        self._setup_max_installments()

    def _refresh_next(self, validation_ok=True):
        validation_ok = validation_ok and self.model.installments_number
        self.wizard.refresh_next(validation_ok)

    def _setup_max_installments(self):
        type = self._selected_type
        maximum = 1

        if type == CreditCardData.TYPE_CREDIT_INSTALLMENTS_STORE:
            maximum = self.method.max_installments
        elif type == CreditCardData.TYPE_CREDIT_INSTALLMENTS_PROVIDER:
            maximum = self.model.provider.max_installments

        if maximum > 1:
            minimum = 2
        else:
            minimum = 1

        self.installments_number.set_range(minimum, maximum)

    def _setup_payments(self):
        provider = self.model.provider
        payment_type = self._selected_type
        due_dates = []
        first_duedate = datetime.datetime.today()
        for i in range(self.model.installments_number):
            if first_duedate.day > provider.closing_day:
                first_duedate += relativedelta(months=+1)
            due_dates.append(first_duedate.replace(day=provider.payment_day))
            first_duedate += relativedelta(months=+1)

        if isinstance(self._order, PurchaseOrder):
            payments = self.method.create_outpayments(self._payment_group,
                                                     self.total_value, due_dates)
        else:
            payments = self.method.create_inpayments(self._payment_group,
                                                     self.total_value, due_dates)

        operation = self.method.operation
        for payment in payments:
            data = operation.get_card_data_by_payment(payment.get_adapted())
            data.card_type = payment_type
            data.provider = provider

    def _get_credit_providers(self):
        return PersonAdaptToCreditProvider.get_card_providers(
            self.method.get_connection())

    #
    #   Callbacks
    #

    def on_credit_provider__changed(self, combo):
        self._setup_max_installments()


class _MultipleMethodEditor(BaseEditor):
    """A generic editor that attaches a payment method slave in a toplevel
    window.
    """
    gladefile = 'HolderTemplate'
    model_type = PaymentGroup
    model_name = _(u'Payment')
    size = (600, 375)

    def __init__(self, wizard, parent, conn, order, payment_method,
                 outstanding_value=currency(0)):
        BaseEditor.__init__(self, conn, order.group)

        self._method = payment_method
        dsm = get_utility(IDomainSlaveMapper)
        slave_class = dsm.get_slave_class(self._method)
        assert slave_class

        self.conn.savepoint('before_payment_creation')

        #FIXME: This is a workaround to make the slave_class to ignore the
        #       payments created previously.
        class _InnerSlaveClass(slave_class):
            def get_created_adapted_payments(self):
                return []

        self.slave = _InnerSlaveClass(wizard, parent, self.conn, order,
                                      self._method, outstanding_value)
        #FIXME: We need to control how many payments could be created, since
        #       we are ignoring the payments created previously.
        payments = order.group.get_valid_payments().filter(
                                        Payment.q.methodID == self._method.id)
        max_installments = self._method.max_installments - payments.count()
        self.slave.installments_number.set_range(1, max_installments)

        self.attach_slave('place_holder', self.slave)

    def on_confirm(self):
        self.slave.finish()
        return True

    def on_cancel(self):
        self.conn.rollback_to_savepoint('before_payment_creation')
        return False


class MultipleMethodSlave(BaseEditorSlave):
    """A base payment method slave for multiple payments."""
    gladefile = 'MultipleMethodSlave'
    model_type = object

    def __init__(self, wizard, parent, conn, order, payment_method,
                 outstanding_value=currency(0)):
        # We need a temporary object to hold the value that will be read from
        # the user. We will set a proxy with this temporary object to help
        # with the validation.
        self._holder = Settable(value=Decimal(0))
        self._wizard = wizard
        # 'money' is the default payment method and it is always avaliable.
        self._method = PaymentMethod.get_by_name(conn, 'money')

        BaseEditorSlave.__init__(self, conn, order)
        self._outstanding_value = (outstanding_value or
                                   self._get_total_amount())
        self._setup_widgets()

    def setup_proxies(self):
        self._proxy = self.add_proxy(self._holder, ['base_value',])

    # The two methods below are required to be a payment method slave without
    # inheriting BasePaymentMethodSlave.

    def update_view(self):
        # The user can only confirm the payments if there is no value left.
        can_confirm = self.can_confirm()

        self._wizard.refresh_next(can_confirm)
        # If this is a sale wizard, we cannot go back after payments have
        # started being created.
        if len(self.payments) > 0:
            self._wizard.disable_back()

    def finish(self):
        # All the payments are created in slaves.
        pass

    #
    # Private
    #

    def _get_total_amount(self):
        if isinstance(self.model, Sale):
            return self.model.get_total_sale_amount()
        elif isinstance(self.model, PaymentRenegotiation):
            return self.model.total
        elif isinstance(self.model, PurchaseOrder):
            # if we have receivings, consider the receiving amount instead.
            receivings = self.model.get_receiving_orders()
            total = sum([r.get_total() for r in receivings], 0)
            return currency(total or self.model.get_purchase_total())
        else:
            raise TypeError

    def _setup_widgets(self):
        self.remove_button.hide()
        self.cash_radio.connect('toggled', self._on_method__toggled)
        self.cash_radio.set_data('method', self._method)
        for method in ['bill', 'check', 'card', 'store_credit']:
            self._add_method(PaymentMethod.get_by_name(self.conn, method))

        self.payments.set_columns(self._get_columns())
        self.payments.add_list(self.model.group.get_items())

        self.total_value.set_bold(True)
        self.received_value.set_bold(True)
        self.missing_value.set_bold(True)
        self.total_value.update(self._outstanding_value)
        self.remove_button.set_sensitive(False)
        self._update_values()

    def _update_values(self):
        payments = self.model.group.get_valid_payments()
        total_payments = payments.sum('value') or Decimal(0)
        self._outstanding_value -= total_payments

        if self._outstanding_value > 0:
            self.base_value.update(self._outstanding_value)
        else:
            self.base_value.update(0)
            self._outstanding_value = 0

        self.received_value.update(total_payments)
        self._update_missing_or_change_value()
        self.base_value.grab_focus()

    def _update_missing_or_change_value(self):
        received = self.received_value.read()
        if received == ValueUnset:
            received = currency(0)
        value = received - self.total_value.read()
        self.missing_value.update(abs(value))
        if value <= 0:
            self.missing_change.set_text(_(u'Missing:'))
        else:
            self.missing_change.set_text(_(u'Change:'))

    def _get_columns(self):
        return [Column('description', title=_(u'Description'), data_type=str,
                        expand=True, sorted=True),
                Column('status_str', title=_('Status'), data_type=str,
                       width=80),
                Column('value', title=_(u'Value'), data_type=currency),
                Column('due_date', title=_('Due Date'),
                        data_type=datetime.date),]

    def _add_method(self, payment_method):
        if not payment_method.is_active:
            return

        # bill and store_credit payment method is not allowed without a client.
        if (payment_method.method_name == 'bill' or
            payment_method.method_name == 'store_credit'):
            if (not isinstance(self.model, PurchaseOrder) and
                self.model.client is None):
                return
            elif (isinstance(self.model, PurchaseOrder) and
                  payment_method.method_name == 'store_credit'):
                return


        radio = gtk.RadioButton(self.cash_radio, payment_method.description)
        self.methods_box.pack_start(radio)
        radio.connect('toggled', self._on_method__toggled)
        radio.set_data('method', payment_method)
        radio.show()

    def _can_add_payment(self):
        payments = self.model.group.get_valid_payments()
        payment_count = payments.filter(
            Payment.q.methodID==self._method.id).count()

        if payment_count >= self._method.max_installments:
            info(_(u'You can not add more payments using the %s '
                   'payment method.') % self._method.description)
            return False

        if self._method.method_name == 'store_credit':
            client = self.model.client
            credit = client.remaining_store_credit
            total = self._holder.value

            if credit < total:
                info(_(u"Client %s does not have enought credit left.") % \
                     client.person.name)
                return False

        return True

    def _add_payment(self):
        assert self._method

        if not self._can_add_payment():
            return

        if self._method.method_name == 'money':
            self._setup_cash_payment()

        # We are about to create payments, so we need to consider the fiscal
        # printer and its operations.
        # See salewizard.SalesPersonStep.on_next_step for details.
        # (We only emit this event for sales.)
        if not isinstance(self.model, PurchaseOrder):
            retval = CreatePaymentEvent.emit(self._method, self.model)
        else:
            retval = None

        if retval is None or retval == CreatePaymentStatus.UNHANDLED:
            if not self._method.method_name == 'money':
                self._run_payment_editor()

        self._update_payment_list()
        self.update_view()

    def _remove_payment(self, payment):
        if payment.is_preview():
            payment.group.remove_item(payment)
            Payment.delete(payment.id, connection=self.conn)
        elif payment.is_paid():
            entry = PaymentChangeHistory(payment=payment,
                             change_reason=_('Payment renegotiated'),
                             connection=self.conn)
            payment.set_not_paid(entry)
            entry.new_status = Payment.STATUS_CANCELLED
            payment.cancel()
        else:
            payment.cancel()

        self._update_payment_list()
        self.update_view()

    def _setup_cash_payment(self):
        has_change_value = self._holder.value - self._outstanding_value > 0
        if has_change_value:
            payment_value = self._outstanding_value
        else:
            payment_value = self._holder.value

        if isinstance(self.model, PurchaseOrder):
            payment = self._method.create_outpayment(
                self.model.group, payment_value).get_adapted()
        else:
            payment = self._method.create_inpayment(
                self.model.group, payment_value).get_adapted()
        # We have to modify the payment, so the fiscal printer can calculate
        # and print the change.
        payment.base_value = self._holder.value

        return True

    def _update_payment_list(self):
        # We reload all the payments each time we update the list. This will
        # avoid the validation of each payment (add or update) and allow us to
        # rename the payments at runtime.
        self.payments.clear()
        payment_group = self.model.group
        payments = payment_group.get_items()
        npayments = payments.count()

        for i, payment in enumerate(payments.orderBy('id')):
            description = payment.method.describe_payment(payment_group, i+1,
                                                          npayments)
            payment.description = description
            self.payments.append(payment)

        self._update_values()

    def _run_payment_editor(self):
        if self._wizard:
            toplevel = self._wizard.get_toplevel()
        else:
            toplevel = None
        retval = run_dialog(_MultipleMethodEditor, toplevel, self._wizard,
                            self, self.conn, self.model, self._method,
                            self._holder.value)
        return retval

    #
    #   Public API
    #

    def enable_remove(self):
        self.remove_button.show()

    def can_confirm(self):
        return self._outstanding_value == 0

    #
    # Callbacks
    #

    def _on_method__toggled(self, radio):
        if not radio.get_active():
            return

        self._method = radio.get_data('method')

    def on_add_button__clicked(self, widget):
        self._add_payment()

    def on_remove_button__clicked(self, button):
        payments = self.payments.get_selected_rows()
        for payment in payments:
            if not payment.is_cancelled():
                self._remove_payment(payment)

    def on_payments__selection_changed(self, objectlist, payment):
        has_payments = len(objectlist) > 0
        self.remove_button.set_sensitive(has_payments)

    def on_base_value__validate(self, entry, value):
        retval = None
        if value < 0:
            retval = ValidationError(_(u'The value must be greater than zero.'))

        if self._outstanding_value < 0:
            self._outstanding_value = 0

        is_money_method = self._method and self._method.method_name == 'money'
        if self._outstanding_value - value < 0 and not is_money_method:
            retval = ValidationError(_(u'The value must be lesser than the '
                                       'missing value.'))

        if not value and self._outstanding_value > 0:
            retval = ValidationError(_(u'You must provide a payment value.'))

        self._holder.value = value
        self._update_missing_or_change_value()
        can_add_payment = retval is None and self._outstanding_value > 0
        self.add_button.set_sensitive(can_add_payment)
        return retval


def register_payment_slaves():
    dsm = get_utility(IDomainSlaveMapper)
    conn = get_connection()
    for method_name, slave_class in [
        ('bill', BillMethodSlave),
        ('check', CheckMethodSlave),
        ('card', CardMethodSlave),
        ('store_credit', StoreCreditMethodSlave),
        ('multiple', MultipleMethodSlave),]:

        method = PaymentMethod.get_by_name(conn, method_name)
        dsm.register(method, slave_class)
