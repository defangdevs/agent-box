// Only ARM sees this default; the portal never renders it as a form field.
// The prefix guarantees Azure's password character classes. A redeployment
// generates a new password unless the caller supplies its own credential.
@secure()
param generatedPassword string = 'Ab1!${newGuid()}'

@secure()
output password string = generatedPassword
