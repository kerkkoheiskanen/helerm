# -*- coding: utf-8 -*-
# Generated by Django 1.9.5 on 2016-04-16 09:49
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('metarecord', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='function',
            name='function_id',
            field=models.CharField(db_index=True, max_length=16, unique=True, verbose_name='function ID'),
        ),
    ]
