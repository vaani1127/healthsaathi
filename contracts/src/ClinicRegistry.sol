// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// @title ClinicRegistry
/// @notice Records each clinic's checkpoint signing key hash and the address allowed to anchor
/// its checkpoints. Every change needs two of the three admins to send the same call.
contract ClinicRegistry {
    uint8 public constant THRESHOLD = 2;

    struct Clinic {
        bytes32 signerKeyHash;
        address poster;
        bool registered;
    }

    address[3] private _admins;
    mapping(bytes32 clinicId => Clinic) private _clinics;
    mapping(bytes32 clinicId => bytes32[]) private _keyHistory;
    mapping(bytes32 operation => uint8 confirmedMask) public confirmations;
    mapping(bytes32 operation => bool) public executed;

    event Confirmed(bytes32 indexed operation, address indexed admin, uint8 count);
    event ClinicRegistered(bytes32 indexed clinicId, bytes32 signerKeyHash, address poster);
    event KeyRotated(
        bytes32 indexed clinicId, bytes32 oldKeyHash, bytes32 newKeyHash, uint256 version
    );

    error NotAdmin();
    error InvalidAdmins();
    error AlreadyRegistered();
    error NotRegistered();
    error InvalidArgument();
    error AlreadyConfirmed();
    error AlreadyExecuted();

    constructor(address[3] memory admins_) {
        for (uint256 i = 0; i < 3; i++) {
            if (admins_[i] == address(0)) revert InvalidAdmins();
            for (uint256 j = 0; j < i; j++) {
                if (admins_[i] == admins_[j]) revert InvalidAdmins();
            }
        }
        _admins = admins_;
    }

    function admins() external view returns (address[3] memory) {
        return _admins;
    }

    function registerClinic(bytes32 clinicId, bytes32 keyHash, address poster) external {
        if (clinicId == bytes32(0) || keyHash == bytes32(0) || poster == address(0)) {
            revert InvalidArgument();
        }
        if (_clinics[clinicId].registered) revert AlreadyRegistered();
        bytes32 operation = keccak256(abi.encode("registerClinic", clinicId, keyHash, poster));
        if (!_confirm(operation)) return;

        _clinics[clinicId] = Clinic(keyHash, poster, true);
        _keyHistory[clinicId].push(keyHash);
        emit ClinicRegistered(clinicId, keyHash, poster);
    }

    function rotateKey(bytes32 clinicId, bytes32 newKeyHash) external {
        Clinic storage clinic = _clinics[clinicId];
        if (!clinic.registered) revert NotRegistered();
        if (newKeyHash == bytes32(0) || newKeyHash == clinic.signerKeyHash) {
            revert InvalidArgument();
        }
        // The current version is part of the operation, so an old key can be restored later.
        uint256 version = _keyHistory[clinicId].length;
        bytes32 operation = keccak256(abi.encode("rotateKey", clinicId, newKeyHash, version));
        if (!_confirm(operation)) return;

        bytes32 oldKeyHash = clinic.signerKeyHash;
        clinic.signerKeyHash = newKeyHash;
        _keyHistory[clinicId].push(newKeyHash);
        emit KeyRotated(clinicId, oldKeyHash, newKeyHash, version + 1);
    }

    function isRegistered(bytes32 clinicId) external view returns (bool) {
        return _clinics[clinicId].registered;
    }

    function signerKeyHash(bytes32 clinicId) external view returns (bytes32) {
        return _clinics[clinicId].signerKeyHash;
    }

    function posterOf(bytes32 clinicId) external view returns (address) {
        return _clinics[clinicId].poster;
    }

    function keyHistory(bytes32 clinicId) external view returns (bytes32[] memory) {
        return _keyHistory[clinicId];
    }

    /// @return ready True when this confirmation reaches the threshold.
    function _confirm(bytes32 operation) private returns (bool ready) {
        uint8 bit = _adminBit(msg.sender);
        if (executed[operation]) revert AlreadyExecuted();
        uint8 mask = confirmations[operation];
        if (mask & bit != 0) revert AlreadyConfirmed();
        mask |= bit;
        confirmations[operation] = mask;

        uint8 count = _popcount(mask);
        emit Confirmed(operation, msg.sender, count);
        if (count >= THRESHOLD) {
            executed[operation] = true;
            return true;
        }
        return false;
    }

    function _adminBit(address account) private view returns (uint8) {
        for (uint8 i = 0; i < 3; i++) {
            if (_admins[i] == account) return uint8(1) << i;
        }
        revert NotAdmin();
    }

    function _popcount(uint8 mask) private pure returns (uint8 count) {
        while (mask != 0) {
            count += mask & 1;
            mask >>= 1;
        }
    }
}
