"""
.. module:: djstripe.webhooks.

  :synopsis: dj-stripe - Views related to the djstripe app.

.. moduleauthor:: @kavdev, @pydanny, @lskillen, @wahuneke, @dollydagr, @chrissmejia
"""
import logging

from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseServerError
from django.shortcuts import redirect
from django.urls import reverse_lazy, reverse
from django.utils.decorators import method_decorator
from django.utils.http import is_safe_url
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import FormView, DetailView, TemplateView, View
from stripe.error import StripeError

from . import settings as djstripe_settings
from .enums import SubscriptionStatus
from .forms import CancelSubscriptionForm
from .forms import PlanForm
from .mixins import SubscriptionMixin, PaymentsContextMixin
from .models import Customer, WebhookEventTrigger, Plan
from .sync import sync_subscriber


#logger = logging.getLogger(__name__)
from afxcc import settings
logging.config.dictConfig(settings.LOGGING)
logger = logging.getLogger('django')


### --- hacking
from braces.views import FormValidMessageMixin, SelectRelatedMixin

# ============================================================================ #
#                                 Account Views                                #
# ============================================================================ #

class AccountView(LoginRequiredMixin, SubscriptionMixin, PaymentsContextMixin, TemplateView):
    """Shows account details including customer and subscription details."""

    template_name = "djstripe/account.html"


# ============================================================================ #
#                                 Billing Views                                #
# ============================================================================ #

class ChangeCardView(LoginRequiredMixin, PaymentsContextMixin, DetailView):
    """TODO: Needs to be refactored to leverage forms and context data."""

    template_name = "djstripe/change_card.html"

    def get_object(self):
        """
        Return a Customer object.

        Ether returns the Customer object from the current class instance or
        uses get_or_create.
        """
        if hasattr(self, "customer"):
            return self.customer
        self.customer, _created = Customer.get_or_create(
            subscriber=djstripe_settings.subscriber_request_callback(self.request)
        )
        return self.customer

    def post(self, request, *args, **kwargs):
        """TODO: Raise a validation error when a stripe token isn't passed. Should be resolved when a form is used."""
        customer = self.get_object()
        try:
            send_invoice = not customer.default_source
            customer.add_card(
                request.POST.get("stripe_token")
            )
            if send_invoice:
                customer.send_invoice()
            customer.retry_unpaid_invoices()
        except StripeError as exc:
            messages.info(request, "Stripe Error")
            return render(
                request,
                self.template_name,
                {
                    "customer": self.get_object(),
                    "stripe_error": str(exc)
                }
            )
        messages.info(request, "Your card is now updated.")
        return redirect(self.get_post_success_url())

    def get_post_success_url(self):
        """Make it easier to do custom dj-stripe integrations."""
        return reverse("djstripe:account")

class HistoryView(LoginRequiredMixin, SelectRelatedMixin, DetailView):
    """A view used to return customer history of invoices."""

    template_name = "djstripe/history.html"
    model = Customer
    select_related = ["invoice"]

    def get_object(self):
        """Return a Customer object."""
        customer, _created = Customer.get_or_create(
            subscriber=djstripe_settings.subscriber_request_callback(self.request)
        )
        return customer


@method_decorator(csrf_exempt, name="dispatch")
class SyncHistoryView(LoginRequiredMixin, View):
    """TODO: Needs to be refactored to leverage context data."""

    template_name = "djstripe/includes/_history_table.html"

    def post(self, request, *args, **kwargs):
        """Render the template while injecting extra context."""
        return render(
            request,
            self.template_name,
            {"customer": sync_subscriber(djstripe_settings.subscriber_request_callback(request))}
        )



# ============================================================================ #
#                              Subscription Views                              #
# ============================================================================ #

class SubscribeView(LoginRequiredMixin, SubscriptionMixin, TemplateView):
    """A view to render the subscribe template."""

    template_name = "djstripe/subscribe.html"


class CancelSubscriptionView(LoginRequiredMixin, SubscriptionMixin, FormView):
    """A view used to cancel a Customer's subscription."""

    template_name = "djstripe/cancel_subscription.html"
    form_class = CancelSubscriptionForm
    success_url = reverse_lazy("home")
    redirect_url = reverse_lazy("home")

    # messages
    subscription_cancel_message = "Your subscription is now cancelled."
    subscription_status_message = "Your subscription status is now '{status}' until '{period_end}'"

    def get_redirect_url(self):
        """
        Return the URL to redirect to when canceling is successful.
        Looks in query string for ?next, ensuring it is on the same domain.
        """
        next = self.request.GET.get(REDIRECT_FIELD_NAME)

        # is_safe_url() will ensure we don't redirect to another domain
        if next and is_safe_url(next, allowed_hosts=None):
            return next
        else:
            return self.redirect_url

    def form_valid(self, form):
        """Handle canceling the Customer's subscription."""
        customer, _created = Customer.get_or_create(
            subscriber=djstripe_settings.subscriber_request_callback(self.request)
        )

        if not customer.subscription:
            # This will trigger if the customer does not have a subscription,
            # or it is already canceled. Do as if the subscription cancels successfully.
            return self.status_cancel()

        subscription = customer.subscription.cancel()

        if subscription.status == SubscriptionStatus.canceled:
            return self.status_cancel()
        else:
            # If pro-rate, they get some time to stay.
            messages.info(self.request, self.subscription_status_message.format(
                status=subscription.status, period_end=subscription.current_period_end)
            )

        return super().form_valid(form)

    def status_cancel(self):
        """Triggered when the subscription is immediately canceled (not pro-rated)"""
        # If no pro-rate, they get kicked right out.
        messages.info(self.request, self.subscription_cancel_message)
        # logout the user
        auth_logout(self.request)
        # Redirect to next url
        return redirect(self.get_redirect_url())

class ConfirmFormView(LoginRequiredMixin, FormValidMessageMixin, SubscriptionMixin, FormView):
    """A view used to confirm customers into a subscription plan."""

    form_class = PlanForm
    template_name = "djstripe/confirm_form.html"
    success_url = reverse_lazy("djstripe:history")
    form_valid_message = "You are now subscribed!"

    def get(self, request, *args, **kwargs):
        """Override ConfirmFormView GET to perform extra validation.

        - Returns 404 when no plan exists.
        - Redirects to djstripe:subscribe when customer is already subscribed to this plan.
        """
        plan_id = self.kwargs['plan_id']

        if not Plan.objects.filter(djstripe_id=plan_id).exists():
            return HttpResponseNotFound()

        customer, _created = Customer.get_or_create(
            subscriber=djstripe_settings.subscriber_request_callback(self.request)
        )

        if (customer.subscription and str(customer.subscription.plan.djstripe_id) == plan_id and
                customer.subscription.is_valid()):
            message = "You already subscribed to this plan"
            messages.info(request, message, fail_silently=True)
            return redirect("djstripe:subscribe")

        return super(ConfirmFormView, self).get(request, *args, **kwargs)

    def get_context_data(self, *args, **kwargs):
        """Return ConfirmFormView's context with plan_id."""
        context = super(ConfirmFormView, self).get_context_data(**kwargs)
        context['plan'] = Plan.objects.get(djstripe_id=self.kwargs['plan_id'])
        return context

    def post(self, request, *args, **kwargs):
        """
        Handle POST requests.

        Instantiates a form instance with the passed POST variables and
        then checks for validity.
        """
        form_class = self.get_form_class()
        form = self.get_form(form_class)
        if form.is_valid():
            try:
                customer, _created = Customer.get_or_create(
                    subscriber=djstripe_settings.subscriber_request_callback(self.request)
                )
                customer.add_card(self.request.POST.get("stripe_token"))

                formplan = form.cleaned_data["plan"]

                logger.debug(">>>>>> formplan: %s", formplan)

                sub_plan = Plan.objects.get(djstripe_id=formplan.djstripe_id)

                logger.debug(">>>>>> sub_plan: %s", sub_plan)

#                sub_plan = form.cleaned_data["plan"]

                # only add subscription if one doesn't exist
                # if customer.has_active_subscription(sub_plan) is not True:
                #     sub = customer.subscribe(sub_plan)
                # else:
                #     sub = customer.has_active_subscription(sub_plan)

                if customer.subscription is None:
                    sub = customer.subscribe(plan=sub_plan)
                else:
                    sub = customer.subscription

                ## do the equivalent of
                # stripe.SubscriptionItem.create(
                #   subscription="sub_D4WVtaNJuV82hS",
                #   plan="plan_D4WlT1WW8pTVgE",
                # )
                sub_item = sub.subscriptionitems.create(plan=sub_plan) #FIXME is this sane?

                ## Start VPS Provisioning
                VPS.new_VPS(user=request.user, subscription_item=sub_item)

            except StripeError as exc:
                form.add_error(None, str(exc))
                return self.form_invalid(form)
            return self.form_valid(form)
        else:
            return self.form_invalid(form)


class ChangePlanView(LoginRequiredMixin, FormValidMessageMixin, SubscriptionMixin, FormView):
    """
    A view used to change a Customers plan.

    TODO: Work in a trial_days kwarg.

    Also, this should be combined with ConfirmFormView.
    """

    form_class = PlanForm
    template_name = "djstripe/confirm_form.html"
    success_url = reverse_lazy("djstripe:history")
    form_valid_message = "You've just changed your plan!"

    def post(self, request, *args, **kwargs):
        """Handle a Customer changing a plan.

        Handles upgrading a plan as well. Throws an error when Customer is not subscribed to any plan.
        """
        form = PlanForm(request.POST)

        customer, _created = Customer.get_or_create(
            subscriber=djstripe_settings.subscriber_request_callback(self.request)
        )

        if not customer.subscription:
            form.add_error(None, "You must already be subscribed to a plan before you can change it.")
            return self.form_invalid(form)

        if form.is_valid():
            try:
                selected_plan = form.cleaned_data["plan"]

                # When a customer upgrades their plan, and DJSTRIPE_PRORATION_POLICY_FOR_UPGRADES is set to True,
                # we force the proration of the current plan and use it towards the upgraded plan,
                # no matter what DJSTRIPE_PRORATION_POLICY is set to.
                if djstripe_settings.PRORATION_POLICY_FOR_UPGRADES:
                    # Is it an upgrade?
                    if selected_plan.amount > customer.subscription.plan.amount:
                        customer.subscription.update(plan=selected_plan, prorate=True)
                    else:
                        customer.subscription.update(plan=selected_plan)
                else:
                    customer.subscription.update(plan=selected_plan)
            except StripeError as exc:
                form.add_error(None, str(exc))
                return self.form_invalid(form)
            return self.form_valid(form)
        else:
            return self.form_invalid(form)




# ============================================================================ #
#                                 Web Services                                 #
# ============================================================================ #


@method_decorator(csrf_exempt, name="dispatch")
class ProcessWebhookView(View):
    """
    A Stripe Webhook handler view.

    This will create a WebhookEventTrigger instance, verify it,
    then attempt to process it.

    If the webhook cannot be verified, returns HTTP 400.

    If an exception happens during processing, returns HTTP 500.
    """

    def post(self, request):
        if "HTTP_STRIPE_SIGNATURE" not in request.META:
            # Do not even attempt to process/store the event if there is
            # no signature in the headers so we avoid overfilling the db.
            return HttpResponseBadRequest()

        trigger = WebhookEventTrigger.from_request(request)

        if trigger.exception:
            # An exception happened, return 500
            return HttpResponseServerError()

        if trigger.is_test_event:
            # Since we don't do signature verification, we have to skip trigger.valid
            return HttpResponse("Test webhook successfully received!")

        if not trigger.valid:
            # Webhook Event did not validate, return 400
            return HttpResponseBadRequest()

        return HttpResponse(str(trigger.id))
