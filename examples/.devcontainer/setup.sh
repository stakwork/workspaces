#!/bin/bash

cd /workspaces/my-project

bundle config set without 'production'

bundle install

echo "creating DB...." >> /workspaces/app_setup.log

rails db:create db:schema:load

echo "Seeding DB...." >> /workspaces/app_setup.log

rails db:seed
