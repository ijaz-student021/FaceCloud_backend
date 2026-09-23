"""review_membership()'s "reject" action has always set state=REJECTED with no
group requirement, but this constraint only exempted state=NEEDS_REVIEW from
needing one -- a real, if previously unreachable (the automated pipeline
always attaches a candidate group whenever it lands in NEEDS_REVIEW), gap
between the model and the code's own intent. Now also exempts REJECTED.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('identities', '0005_backfill_group_organization'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='identitymembership',
            name='identity_group_required_except_review',
        ),
        migrations.AddConstraint(
            model_name='identitymembership',
            constraint=models.CheckConstraint(condition=models.Q(('identity_group__isnull', False), ('state__in', ['needs_review', 'rejected']), _connector='OR'), name='identity_group_required_except_review'),
        ),
    ]
