// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";

import {AuditAnchor} from "../src/AuditAnchor.sol";
import {ClinicRegistry} from "../src/ClinicRegistry.sol";
import {ConsentRegistry} from "../src/ConsentRegistry.sol";

/// Deploys the three contracts and writes their addresses to deployments/<DEPLOY_NETWORK>.json.
///
/// Required env: DEPLOY_NETWORK, REGISTRY_ADMIN_1, REGISTRY_ADMIN_2, REGISTRY_ADMIN_3.
/// The deployer key or sender is passed to `forge script` on the command line, never stored here.
contract Deploy is Script {
    function run()
        external
        returns (ClinicRegistry registry, AuditAnchor anchor, ConsentRegistry consent)
    {
        string memory network = vm.envString("DEPLOY_NETWORK");
        address[3] memory admins = [
            vm.envAddress("REGISTRY_ADMIN_1"),
            vm.envAddress("REGISTRY_ADMIN_2"),
            vm.envAddress("REGISTRY_ADMIN_3")
        ];

        vm.startBroadcast();
        registry = new ClinicRegistry(admins);
        anchor = new AuditAnchor(registry);
        consent = new ConsentRegistry(registry);
        vm.stopBroadcast();

        string memory key = "deployment";
        vm.serializeString(key, "network", network);
        vm.serializeUint(key, "chainId", block.chainid);
        vm.serializeUint(key, "startBlock", block.number);
        vm.serializeAddress(key, "admins", _asArray(admins));
        vm.serializeAddress(key, "ClinicRegistry", address(registry));
        vm.serializeAddress(key, "ConsentRegistry", address(consent));
        string memory json = vm.serializeAddress(key, "AuditAnchor", address(anchor));
        vm.writeJson(json, string.concat(vm.projectRoot(), "/deployments/", network, ".json"));
    }

    function _asArray(address[3] memory admins) private pure returns (address[] memory out) {
        out = new address[](3);
        for (uint256 i = 0; i < 3; i++) {
            out[i] = admins[i];
        }
    }
}
