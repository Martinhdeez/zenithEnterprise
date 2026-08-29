"""How an account comes into existence.

Provisioning seeds a tenant's system roles, invitations bring a person in, and credential
tokens are the single-use links that let somebody set a password without one being mailed in
clear text. All three run before anybody has a session, which is what makes them a group.
"""
