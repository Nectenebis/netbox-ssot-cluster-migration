# Проверенные интерфейсы

Проверка исходников проведена 2026-10-16; live endpoints заказчика недоступны.

* [netbox-ssot v1.26.0](https://github.com/bl4ko/netbox-ssot/tree/v1.26.0),
  commit `f4cfd66d2f19cb345d8cd2a76e2c5a8fcdcce3b0`:
  `internal/netbox/inventory/add_items.go`, `AddVM`: индекс name + cluster ID,
  `internal/source/vmware/vmware_sync.go`, `syncVM`: поле source и source tags.
  VM source_id не используется как instance UUID; Cluster/Device source_id — MoRef.
  `internal/source/source.go`: type tag `vmware`; source tag настраиваемый,
  default `Source: <source name>`. Source fields/tags reconciler не изменяет.
* [NetBox v4.7.0](https://github.com/netbox-community/netbox/tree/v4.7.0),
  commit `5f06007e4c9bacc93ce17c1e645fc1143d60df3d`:
  `netbox/virtualization/api/serializers_/virtualmachines.py`: API fields
  name/cluster/device/site. `models/virtualmachines.py`, `clean`: устройство
  принадлежит выбранному кластеру и Site соответствует устройству. Имя уникально
  без учёта регистра в cluster + tenant; SSoT игнорирует tenant при поиске, поэтому
  reconciler консервативнее и блокирует конфликт name + cluster для любого tenant.
  Scoped Cluster дополнительная проверка намеренно ограничена `dcim.site`.
* [pynetbox](https://github.com/netbox-community/pynetbox), установленная версия 7.9.0:
  проверены `api`, `Endpoint.all/filter/get`, `Record.__iter__`,
  `core.query.Request.patch`. Используется persistent requests Session,
  pagination offset (pynetbox), явный timeout и PATCH строго с переданным dict.
  `Record.save` не используется, чтобы избежать отправки посторонних полей.
* [pyVmomi](https://github.com/vmware/pyvmomi), установленная версия 9.1.1.0:
  проверены сигнатура `pyVim.connect.SmartConnect` (sslContext,
  httpConnectionTimeout), типы `vim.AboutInfo.instanceUuid`,
  `vim.vm.ConfigInfo.instanceUuid/uuid/template` и
  `vmodl.query.PropertyCollector.RetrieveOptions.maxObjects`.
  Используются ContainerView и RetrievePropertiesEx/ContinueRetrievePropertiesEx,
  view уничтожается, незавершённая pagination отменяется, соединение Disconnect.
* Семантика UUID: [VirtualMachineConfigInfo в vSphere Web Services API](https://developer.broadcom.com/xapis/vsphere-web-services-api/latest/vim.vm.ConfigInfo.html),
  [AboutInfo](https://developer.broadcom.com/xapis/vsphere-web-services-api/latest/vim.AboutInfo.html).
  Не гарантируется сохранение VM UUID при любом способе cross-vCenter migration.

Ограничения: явные ID placement должны поддерживаться оператором; неоднозначность
не разрешается по DNS, одному имени или BIOS UUID. Гонки между отдельными HTTPS
запросами исключаются общей операционной сериализацией writers, не REST API.
