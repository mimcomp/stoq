# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2005-2007 Async Open Source <http://www.async.com.br>
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
##
##
""" Purchase wizard definition """

import datetime

from kiwi.datatypes import currency, ValidationError
from kiwi.ui.widgets.list import Column
from stoqdrivers.enum import PaymentMethodType

from stoqlib.database.runtime import get_current_branch
from stoqlib.lib.translation import stoqlib_gettext
from stoqlib.lib.defaults import INTERVALTYPE_MONTH
from stoqlib.lib.parameters import sysparam
from stoqlib.lib.validators import format_quantity
from stoqlib.gui.base.wizards import WizardEditorStep, BaseWizard
from stoqlib.gui.printing import print_report
from stoqlib.gui.wizards.personwizard import run_person_role_dialog
from stoqlib.gui.wizards.abstractwizard import SellableItemStep
from stoqlib.gui.editors.personeditor import SupplierEditor, TransporterEditor
from stoqlib.gui.editors.producteditor import ProductEditor
from stoqlib.gui.slaves.purchaseslave import PurchasePaymentSlave
from stoqlib.gui.slaves.saleslave import DiscountSurchargeSlave
from stoqlib.domain.sellable import ASellable
from stoqlib.domain.person import Person
from stoqlib.domain.purchase import PurchaseOrder, PurchaseItem
from stoqlib.domain.interfaces import (IBranch, ITransporter, ISupplier,
                                       IPaymentGroup)
from stoqlib.reporting.purchase import PurchaseOrderReport

_ = stoqlib_gettext


#
# Wizard Steps
#


class StartPurchaseStep(WizardEditorStep):
    gladefile = 'StartPurchaseStep'
    model_type = PurchaseOrder
    proxy_widgets = ('open_date',
                     'order_number',
                     'supplier',
                     'branch',
                     'freight')
    def __init__(self, wizard, conn, model):
        WizardEditorStep.__init__(self, conn, wizard, model)

    def _fill_supplier_combo(self):
        # FIXME: Implement and use IDescribable on PersonAdaptToSupplier
        table = Person.getAdapterClass(ISupplier)
        suppliers = table.get_active_suppliers(self.conn)
        items = [(s.person.name, s) for s in suppliers]
        self.supplier.prefill(items)

    def _fill_branch_combo(self):
        # FIXME: Implement and use IDescribable on PersonAdaptToBranch
        table = Person.getAdapterClass(IBranch)
        branches = table.get_active_branches(self.conn)
        items = [(s.person.name, s) for s in branches]
        self.branch.prefill(items)

    def _setup_widgets(self):
        self._fill_supplier_combo()
        self._fill_branch_combo()
        self._update_widgets()

    def _update_widgets(self):
        has_freight = self.fob_radio.get_active()
        self.freight.set_sensitive(has_freight)
        self._update_freight()

    def _update_freight(self):
        if self.cif_radio.get_active():
            self.model.freight_type = self.model_type.FREIGHT_CIF
        else:
            self.model.freight_type = self.model_type.FREIGHT_FOB

    #
    # WizardStep hooks
    #

    def post_init(self):
        self.open_date.grab_focus()
        self.table.set_focus_chain([self.open_date, self.order_number,
                                    self.branch, self.supplier,
                                    self.radio_hbox, self.freight])
        self.radio_hbox.set_focus_chain([self.cif_radio, self.fob_radio])
        self.register_validate_function(self.wizard.refresh_next)
        self.force_validation()

    def next_step(self):
        return PurchaseItemStep(self.wizard, self, self.conn, self.model)

    def has_previous_step(self):
        return False

    def setup_proxies(self):
        self._setup_widgets()
        self.proxy = self.add_proxy(self.model,
                                    StartPurchaseStep.proxy_widgets)

    #
    # Kiwi callbacks
    #

    def on_cif_radio__toggled(self, *args):
        self._update_widgets()

    def on_fob_radio__toggled(self, *args):
        self._update_widgets()

    def on_supplier_button__clicked(self, *args):
        if run_person_role_dialog(SupplierEditor, self, self.conn,
                                  self.model.supplier):
            self.conn.commit()
            self._setup_supplier_entry()

    def on_open_date__validate(self, widget, date):
        if date < datetime.date.today():
            return ValidationError(
                _("Expected receival date must be set to today or "
                  "a future date"))

class PurchaseItemStep(SellableItemStep):
    """ Wizard step for purchase order's items selection """
    model_type = PurchaseOrder
    item_table = PurchaseItem
    summary_label_text = "<b>%s</b>" % _('Total Ordered:')

    #
    # Helper methods
    #

    def setup_item_entry(self):
        sellables = ASellable.get_unblocked_sellables(self.conn, storable=True)
        self.item.prefill([(sellable.get_description(), sellable)
                           for sellable in sellables])

    def setup_slaves(self):
        SellableItemStep.setup_slaves(self)
        self.hide_add_and_edit_buttons()


    #
    # SellableItemStep virtual methods
    #

    def get_order_item(self, sellable, cost, quantity):
        item = self.model.add_item(sellable, quantity)
        item.cost = cost
        return item

    def get_saved_items(self):
        return list(self.model.get_items())

    def get_columns(self):
        return [
            Column('sellable.description', title=_('Description'),
                   data_type=str, expand=True, searchable=True),
            Column('quantity', title=_('Quantity'), data_type=float, width=90,
                   format_func=format_quantity),
            Column('sellable.unit_description',title=_('Unit'), data_type=str,
                   width=50),
            Column('cost', title=_('Cost'), data_type=currency, width=90),
            Column('total', title=_('Total'), data_type=currency, width=100),
            ]

    #
    # WizardStep hooks
    #

    def post_init(self):
        self.slave.set_editor(ProductEditor)
        self._refresh_next()
        self.product_button.hide()

    def next_step(self):
        return PurchasePaymentStep(self.wizard, self, self.conn, self.model)

class PurchasePaymentStep(WizardEditorStep):
    gladefile = 'PurchasePaymentStep'
    model_iface = IPaymentGroup
    payment_widgets = ('method_combo',)
    order_widgets = ('subtotal_lbl',
                     'total_lbl')

    def __init__(self, wizard, previous, conn, model):
        self.order = model
        pg = IPaymentGroup(model, None)
        if pg:
            model = pg
        else:
            method = PaymentMethodType.BILL
            interval_type = INTERVALTYPE_MONTH
            model = model.addFacet(IPaymentGroup, default_method=int(method),
                                   intervals=1,
                                   interval_type=interval_type,
                                   connection=conn)
        self.slave = None
        self.discount_surcharge_slave = None
        WizardEditorStep.__init__(self, conn, wizard, model, previous)

    def _setup_widgets(self):
        items = [(_('Bill'), int(PaymentMethodType.BILL)),
                 (_('Check'), int(PaymentMethodType.CHECK)),
                 (_('Money'), int(PaymentMethodType.MONEY))]
        self.method_combo.prefill(items)

    def _update_payment_method_slave(self):
        holder_name = 'method_slave_holder'
        if self.get_slave(holder_name):
            self.detach_slave(holder_name)
        if not self.slave:
            self.slave = PurchasePaymentSlave(self.conn, self.model)
        if self.model.default_method == PaymentMethodType.MONEY:
            self.slave.get_toplevel().hide()
            return
        self.slave.get_toplevel().show()
        self.attach_slave('method_slave_holder', self.slave)

    def _update_totals(self, *args):
        for field_name in ('purchase_subtotal', 'purchase_total'):
            self.order_proxy.update(field_name)

    #
    # WizardStep hooks
    #

    def next_step(self):
        return FinishPurchaseStep(self.wizard, self, self.conn,
                                  self.order)

    def post_init(self):
        self.method_combo.grab_focus()
        self.main_box.set_focus_chain([self.payment_method_hbox,
                                       self.method_slave_holder])
        self.payment_method_hbox.set_focus_chain([self.method_combo])
        self.register_validate_function(self.wizard.refresh_next)
        self.force_validation()

    def setup_proxies(self):
        self._setup_widgets()
        self.order_proxy = self.add_proxy(self.order,
                                          PurchasePaymentStep.order_widgets)
        self.proxy = self.add_proxy(self.model,
                                    PurchasePaymentStep.payment_widgets)

    def setup_slaves(self):
        self._update_payment_method_slave()
        slave_holder = 'discount_surcharge_slave'
        if self.get_slave(slave_holder):
            return
        if not self.wizard.edit_mode:
            self.order.reset_discount_and_surcharge()
        self.discount_surcharge_slave = DiscountSurchargeSlave(self.conn,
                                                               self.order,
                                                               PurchaseOrder)
        self.attach_slave(slave_holder, self.discount_surcharge_slave)
        self.discount_surcharge_slave.connect('discount-changed',
                                           self._update_totals)
        self.discount_surcharge_slave.set_max_discount(100)
        self._update_totals()

    #
    # callbacks
    #

    def on_method_combo__content_changed(self, *args):
        self._update_payment_method_slave()

class FinishPurchaseStep(WizardEditorStep):
    gladefile = 'FinishPurchaseStep'
    model_type = PurchaseOrder
    proxy_widgets = ('salesperson_name',
                     'receival_date',
                     'transporter',
                     'notes')

    def __init__(self, wizard, previous, conn, model):
        WizardEditorStep.__init__(self, conn, wizard, model, previous)

    def _setup_transporter_entry(self):
        # FIXME: Implement and use IDescribable on PersonAdaptToTransporter
        table = Person.getAdapterClass(ITransporter)
        transporters = table.get_active_transporters(self.conn)
        items = [(t.person.name, t) for t in transporters]
        self.transporter.prefill(items)

    def _setup_focus(self):
        self.salesperson_name.grab_focus()
        self.notes.set_accepts_tab(False)

    #
    # WizardStep hooks
    #

    def has_next_step(self):
        return False

    def post_init(self):
        self.salesperson_name.grab_focus()
        self.register_validate_function(self.wizard.refresh_next)
        self.force_validation()

    def setup_proxies(self):
        self._setup_transporter_entry()
        self.proxy = self.add_proxy(self.model, self.proxy_widgets)

    #
    # Kiwi callbacks
    #

    def on_receival_date__validate(self, widget, date):
        if date < datetime.date.today():
            return ValidationError(_("Expected receival date must be set to a future date"))

    def on_transporter_button__clicked(self, button):
        if run_person_role_dialog(TransporterEditor, self, self.conn,
                                  self.model.transporter):
            self.conn.commit()
            self._setup_transporter_entry()

    def on_print_button__clicked(self, button):
        print_report(PurchaseOrderReport, self.model)


#
# Main wizard
#


class PurchaseWizard(BaseWizard):
    size = (775, 400)

    def __init__(self, conn, model=None, edit_mode=False):
        title = self._get_title(model)
        model = model or self._create_model(conn)
        if model.status != PurchaseOrder.ORDER_PENDING:
            raise ValueError('Invalid order status. It should '
                             'be ORDER_PENDING')
        first_step = StartPurchaseStep(self, conn, model)
        BaseWizard.__init__(self, conn, first_step, model, title=title,
                            edit_mode=edit_mode)

    def _get_title(self, model=None):
        if not model:
            return _('New Order')
        return _('Edit Order')

    def _create_model(self, conn):
        supplier = sysparam(conn).SUGGESTED_SUPPLIER
        branch = get_current_branch(conn)
        status = PurchaseOrder.ORDER_PENDING
        return PurchaseOrder(supplier=supplier, branch=branch, status=status,
                             connection=conn)

    #
    # WizardStep hooks
    #

    def finish(self):
        if not self.model.get_valid():
            self.model.set_valid()
        self.retval = self.model
        self.close()
